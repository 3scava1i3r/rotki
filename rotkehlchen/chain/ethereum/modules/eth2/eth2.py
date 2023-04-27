import logging
import re
from collections import defaultdict
from typing import TYPE_CHECKING, Literal, Optional, Union

import gevent
from pysqlcipher3 import dbapi2 as sqlcipher

from rotkehlchen.accounting.structures.balance import Balance
from rotkehlchen.accounting.structures.eth2 import EthBlockEvent, EthWithdrawalEvent
from rotkehlchen.accounting.structures.types import HistoryEventSubType, HistoryEventType
from rotkehlchen.constants.assets import A_ETH
from rotkehlchen.constants.misc import ONE
from rotkehlchen.db.eth2 import DBEth2
from rotkehlchen.db.filtering import EvmEventFilterQuery
from rotkehlchen.db.history_events import DBHistoryEvents
from rotkehlchen.errors.api import PremiumPermissionError
from rotkehlchen.errors.misc import InputError, RemoteError
from rotkehlchen.fval import FVal
from rotkehlchen.inquirer import Inquirer
from rotkehlchen.logging import RotkehlchenLogsAdapter
from rotkehlchen.premium.premium import Premium
from rotkehlchen.types import ChecksumEvmAddress, Eth2PubKey, Timestamp
from rotkehlchen.user_messages import MessagesAggregator
from rotkehlchen.utils.interfaces import EthereumModule
from rotkehlchen.utils.misc import from_gwei, ts_now, ts_sec_to_ms

from .constants import (
    CPT_ETH2,
    FREE_VALIDATORS_LIMIT,
    LAST_WITHDRAWALS_QUERY_TS,
    VALIDATOR_STATS_QUERY_BACKOFF_EVERY_N_VALIDATORS,
    VALIDATOR_STATS_QUERY_BACKOFF_TIME,
    VALIDATOR_STATS_QUERY_BACKOFF_TIME_RANGE,
)
from .structures import (
    DEPOSITING_VALIDATOR_PERFORMANCE,
    Eth2Validator,
    ValidatorDailyStats,
    ValidatorDetails,
    ValidatorID,
)
from .utils import scrape_validator_daily_stats, scrape_validator_withdrawals

if TYPE_CHECKING:
    from rotkehlchen.chain.ethereum.node_inquirer import EthereumInquirer
    from rotkehlchen.db.dbhandler import DBHandler
    from rotkehlchen.db.drivers.gevent import DBCursor
    from rotkehlchen.db.filtering import Eth2DailyStatsFilterQuery
    from rotkehlchen.externalapis.beaconchain import BeaconChain

logger = logging.getLogger(__name__)
log = RotkehlchenLogsAdapter(logger)


class Eth2(EthereumModule):
    """Module representation for Eth2"""

    def __init__(
            self,
            ethereum_inquirer: 'EthereumInquirer',
            database: 'DBHandler',
            premium: Optional[Premium],
            msg_aggregator: MessagesAggregator,
            beaconchain: 'BeaconChain',
    ) -> None:
        self.database = database
        self.premium = premium
        self.ethereum = ethereum_inquirer
        self.msg_aggregator = msg_aggregator
        self.beaconchain = beaconchain
        self.last_stats_query_ts = 0
        self.last_withdrawals_query_ts = 0
        self.validator_stats_queried = 0
        self.validator_withdrawals_queried = 0
        self.deposits_pubkey_re = re.compile(r'.*validator with pubkey (.*)\. Deposit.*')

    def fetch_and_update_eth1_validator_data(
            self,
            addresses: list[ChecksumEvmAddress],
    ) -> list[ValidatorID]:
        """Query all eth1 addresses for their validators and any newly detected validators
        are added to the DB.

        Returns the list of all tracked validators. It's ValidatorID  since
        for validators that are in the deposit queue we don't get a finalized validator index yet.
        So index may be missing for some validators.
        This is the only function that will also return validators in the deposit queue.

        May raise:
        - RemoteError
        """
        dbeth2 = DBEth2(self.database)
        with self.database.conn.read_ctx() as cursor:
            all_validators_ids = [
                ValidatorID(
                    index=eth2_validator.index,
                    public_key=eth2_validator.public_key,
                    ownership_proportion=eth2_validator.ownership_proportion,
                )
                for eth2_validator in dbeth2.get_validators(cursor)
            ]

        new_validators = []
        tracked_pubkeys = {validator_id.public_key for validator_id in all_validators_ids}
        for address in addresses:
            validators = self.beaconchain.get_eth1_address_validators(address)
            if len(validators) == 0:
                continue

            new_validator_ids = [x for x in validators if x.public_key not in tracked_pubkeys]
            new_validators.extend([
                Eth2Validator(index=validator_id.index, public_key=validator_id.public_key, ownership_proportion=ONE)  # noqa: E501
                for validator_id in new_validator_ids if validator_id.index is not None
            ])
            tracked_pubkeys.update([x.public_key for x in new_validator_ids])
            all_validators_ids.extend(new_validator_ids)

        with self.database.user_write() as write_cursor:
            dbeth2.add_validators(write_cursor, new_validators)

        return all_validators_ids

    def get_balances(
            self,
            addresses: list[ChecksumEvmAddress],
            fetch_validators_for_eth1: bool,
    ) -> dict[Eth2PubKey, Balance]:
        """
        Returns a mapping of validator public key to eth balance.
        If fetch_validators_for_eth1 is true then each eth1 address is also checked
        for the validators it has deposited and the deposits are fetched.

        May Raise:
        - RemoteError from beaconcha.in api
        """
        usd_price = Inquirer().find_usd_price(A_ETH)
        dbeth2 = DBEth2(self.database)
        balance_mapping: dict[Eth2PubKey, Balance] = defaultdict(Balance)
        validators: Union[list[ValidatorID], list[Eth2Validator]]
        if fetch_validators_for_eth1:
            validators = self.fetch_and_update_eth1_validator_data(addresses)
        else:
            with self.database.conn.read_ctx() as cursor:
                validators = dbeth2.get_validators(cursor)

        if validators == []:
            return {}  # nothing detected

        pubkeys = []
        index_to_pubkey = {}
        index_to_ownership = {}
        for validator in validators:
            # create a mapping of indices to pubkeys since the performance call returns indices
            if validator.index is not None:
                index_to_pubkey[validator.index] = validator.public_key
                pubkeys.append(validator.public_key)
            index_to_ownership[validator.index] = validator.ownership_proportion

        # Get current balance of all validators. This may miss some balance if it's
        # in the deposit queue but it's too much work to get it right and should be
        # visible as soon as deposit clears the queue
        performance = self.beaconchain.get_performance(pubkeys)
        for validator_index, entry in performance.items():
            pubkey = index_to_pubkey.get(validator_index)
            if pubkey is None:
                log.error(f'At eth2 get_balances could not find matching pubkey for validator index {validator_index}')  # noqa: E501
                continue  # should not happen
            ownership_proportion = index_to_ownership.get(validator_index, ONE)
            amount = from_gwei(entry.balance) * ownership_proportion
            balance_mapping[pubkey] += Balance(amount, amount * usd_price)

        return balance_mapping

    def get_details(
            self,
            addresses: list[ChecksumEvmAddress],
    ) -> list[ValidatorDetails]:
        """Go through the list of eth1 addresses and find all eth2 validators associated
        with them along with their details.

        May raise RemoteError due to beaconcha.in API"""
        indices = []
        index_to_address = {}
        index_to_pubkey = {}
        pubkey_to_index = {}
        result = []
        assert self.beaconchain.db is not None, 'Beaconchain db should be populated'
        address_validators = []

        for address in addresses:
            validators = self.beaconchain.get_eth1_address_validators(address)
            for validator in validators:
                if validator.index is None:
                    # for validators that are so early in the depositing queue that no
                    # validator index is confirmed yet let's return only the most basic info
                    result.append(ValidatorDetails(
                        validator_index=None,
                        public_key=validator.public_key,
                        eth1_depositor=address,
                        performance=DEPOSITING_VALIDATOR_PERFORMANCE,
                    ))
                    continue

                index_to_address[validator.index] = address
                address_validators.append(Eth2Validator(index=validator.index, public_key=validator.public_key, ownership_proportion=ONE))  # noqa: E501

        # make sure all validators we deal with are saved in the DB
        dbeth2 = DBEth2(self.database)
        with self.database.user_write() as write_cursor:
            dbeth2.add_validators(write_cursor, address_validators)
        with self.database.conn.read_ctx() as cursor:
            # Also get all manually input validators
            all_validators = dbeth2.get_validators(cursor)

        for v in all_validators:  # populate mappings for validators we know of
            index_to_pubkey[v.index] = v.public_key
            pubkey_to_index[v.public_key] = v.index
            indices.append(v.index)

        # Check all our currently decoded deposits for known public keys and map to depositors
        pubkey_to_depositor = self._get_saved_pubkey_to_deposit_address()
        for public_key, depositor in pubkey_to_depositor.items():
            index = pubkey_to_index.get(public_key)
            if index is None:
                continue

            index_to_address[index] = depositor

        # Get current balance of all validator indices
        performance_result = self.beaconchain.get_performance(indices)
        for validator_index, entry in performance_result.items():
            result.append(ValidatorDetails(  # depositor can be None for manually input validator
                validator_index=validator_index,
                public_key=index_to_pubkey[validator_index],
                eth1_depositor=index_to_address.get(validator_index),
                performance=entry,
            ))

        # Performance call does not return validators that are not active and are still depositing
        depositing_indices = set(index_to_address.keys()) - set(performance_result.keys())
        for index in depositing_indices:
            result.append(ValidatorDetails(  # depositor can be None for manually input validator
                validator_index=index,
                public_key=index_to_pubkey[index],
                eth1_depositor=index_to_address.get(index),
                performance=DEPOSITING_VALIDATOR_PERFORMANCE,
            ))

        return result

    def _get_saved_pubkey_to_deposit_address(self) -> dict[Eth2PubKey, ChecksumEvmAddress]:
        """Read the decoded DB history events to find out public keys -> deposit addresses

        This will just return the data for the currently decoded history events.
        And also has a really ugly hack since it takes the public key from the notes.
        """
        dbevents = DBHistoryEvents(self.database)
        with self.database.conn.read_ctx() as cursor:
            deposit_events = dbevents.get_history_events(
                cursor=cursor,
                filter_query=EvmEventFilterQuery.make(
                    event_types=[HistoryEventType.STAKING],
                    event_subtypes=[HistoryEventSubType.DEPOSIT_ASSET],
                    counterparties=[CPT_ETH2],
                ),
                has_premium=True,  # need all events here
            )

        result = {}
        for event in deposit_events:
            if event.notes is None:
                log.error(  # should not really happen
                    f'Could not match the pubkey extraction regex for {event} '
                    f'due to absence of notes',
                )
                continue

            match = self.deposits_pubkey_re.match(event.notes)
            if match is None:
                log.error(f'Could not match the pubkey extraction regex for "{event.notes}"')
                continue  # should not really happen though

            groups = match.groups()
            if len(groups) != 1:
                log.error(f'Could not match group for pubkey extraction regex for "{event.notes}"')
                continue  # should not really happen though

            result[Eth2PubKey(groups[0])] = event.location_label

        return result  # type: ignore  # location_label is set for this event

    def _maybe_backoff_beaconchain(self, now: Timestamp, name: Literal['stats', 'withdrawals']) -> None:  # noqa: E501
        should_backoff = (
            now - getattr(self, f'last_{name}_query_ts') < VALIDATOR_STATS_QUERY_BACKOFF_TIME_RANGE and  # noqa: E501
            getattr(self, f'validator_{name}_queried') >= VALIDATOR_STATS_QUERY_BACKOFF_EVERY_N_VALIDATORS  # noqa: E501
        )
        if should_backoff:
            log.debug(
                f'Queried {getattr(self, f"validator_{name}_queried")} validators in the last '
                f'{VALIDATOR_STATS_QUERY_BACKOFF_TIME_RANGE} seconds. Backing off for '
                f'{VALIDATOR_STATS_QUERY_BACKOFF_TIME} seconds.',
            )
            setattr(self, f'validator_{name}_queried', 0)
            gevent.sleep(VALIDATOR_STATS_QUERY_BACKOFF_TIME)

    def _query_services_for_validator_daily_stats(
            self,
            to_ts: Timestamp,
    ) -> None:
        """Goes through all saved validators and sees which need to have their stats requeried"""
        now = ts_now()
        dbeth2 = DBEth2(self.database)
        result = dbeth2.get_validators_to_query_for_stats(up_to_ts=to_ts)

        for validator_index, last_ts in result:
            self._maybe_backoff_beaconchain(now=now, name='stats')
            new_stats = scrape_validator_daily_stats(
                validator_index=validator_index,
                last_known_timestamp=last_ts,
            )
            self.validator_stats_queried += 1
            self.last_stats_query_ts = now

            if len(new_stats) != 0:
                dbeth2.add_validator_daily_stats(stats=new_stats)

    def query_services_for_validator_withdrawals(
            self,
            to_ts: Timestamp,
    ) -> None:
        """Goes through all saved validators, sees which need to query withdrawals
        queries them and saves them in the DB.

        May raise:
        - RemoteError due to problems querying beaconcha.in API
        """
        dbeth2 = DBEth2(self.database)
        dbevents = DBHistoryEvents(self.database)
        result = dbeth2.get_validators_to_query_for_withdrawals(up_to_tsms=ts_sec_to_ms(to_ts))

        # first get all validator indices to see which validators have exited and when
        validator_indices = {x[0] for x in result}
        data = self.beaconchain._query_chunked_endpoint(
            indices_or_pubkeys=list(validator_indices),
            module='validator',
            endpoint=None,
        )
        exit_epoch = {}
        now = ts_now()
        for validator_entry in data:
            if validator_entry['status'] != 'exited':
                exit_epoch[validator_entry['validatorindex']] = now  # so that is_exit fails
            else:
                exit_epoch[validator_entry['validatorindex']] = validator_entry['exitepoch']

        # Then fetch latest withdrawals for each
        for validator_index, last_ts in result:
            self._maybe_backoff_beaconchain(now=now, name='withdrawals')
            new_data = scrape_validator_withdrawals(
                validator_index=validator_index,
                last_known_timestamp=last_ts,
            )
            now = ts_now()
            self.validator_withdrawals_queried += 1
            self.last_withdrawals_query_ts = now
            for entry in new_data:
                withdrawal = EthWithdrawalEvent(
                    validator_index=validator_index,
                    timestamp=ts_sec_to_ms(entry[0]),
                    balance=Balance(amount=entry[2]),
                    withdrawal_address=entry[1],
                    is_exit=entry[0] >= exit_epoch[validator_index],
                )
                try:
                    with self.database.user_write() as write_cursor:
                        dbevents.add_history_event(write_cursor, event=withdrawal)
                except sqlcipher.IntegrityError as e:  # pylint: disable=no-member
                    log.error(f'Could not write withdrawal: {withdrawal} to the DB due to {str(e)}')  # noqa: E501

        with self.database.user_write() as write_cursor:
            self.database.update_used_query_range(  # update last withdrawal query timestamp
                write_cursor=write_cursor,
                name=LAST_WITHDRAWALS_QUERY_TS,
                start_ts=Timestamp(0),
                end_ts=now,
            )

    def get_validator_daily_stats(
            self,
            cursor: 'DBCursor',
            filter_query: 'Eth2DailyStatsFilterQuery',
            only_cache: bool,
    ) -> tuple[list[ValidatorDailyStats], int, FVal]:
        """Gets the daily stats eth2 validators depending on the given filter.

        This won't detect new validators

        Will query for new validator daily stats if only_cache is False.

        May raise:
        - RemoteError due to problems with beaconcha.in
        """
        if only_cache is False:
            self._query_services_for_validator_daily_stats(to_ts=filter_query.to_ts)

        dbeth2 = DBEth2(self.database)
        return dbeth2.get_validator_daily_stats_and_limit_info(cursor, filter_query=filter_query)

    def add_validator(
            self,
            validator_index: Optional[int],
            public_key: Optional[Eth2PubKey],
            ownership_proportion: FVal,
    ) -> None:
        """Adds the given validator to the DB. Due to marshmallow here at least
        either validator_index or public key is not None.

        May raise:
        - RemoteError if there is a problem with querying beaconcha.in for more info
        - InputError if the validator is already in the DB
        """
        valid_index: int
        valid_pubkey: Eth2PubKey
        dbeth2 = DBEth2(self.database)
        if self.premium is None:
            with self.database.conn.read_ctx() as cursor:
                tracked_validators = dbeth2.get_validators(cursor)
            if len(tracked_validators) >= FREE_VALIDATORS_LIMIT:
                raise PremiumPermissionError(
                    f'Adding validator {validator_index} {public_key} would take you '
                    f'over the free limit of {FREE_VALIDATORS_LIMIT} for tracked validators',
                )

        if validator_index is not None and public_key is not None:
            valid_index = validator_index
            valid_pubkey = public_key
            with self.database.conn.read_ctx() as cursor:
                if dbeth2.validator_exists(cursor, field='validator_index', arg=valid_index):
                    raise InputError(f'Validator {valid_index} already exists in the DB')
        else:  # we are missing one of the 2
            if validator_index is None:
                field = 'public_key'
                arg = public_key
            else:  # we should have valid index
                field = 'validator_index'
                arg = validator_index  # type: ignore

            with self.database.conn.read_ctx() as cursor:
                if dbeth2.validator_exists(cursor, field=field, arg=arg):  # type: ignore
                    raise InputError(f'Validator {arg} already exists in the DB')

            # at this point we gotta query for one of the two
            result = self.beaconchain._query(
                module='validator',
                endpoint=None,
                encoded_args=arg,  # type: ignore
            )
            if not isinstance(result, dict):
                raise RemoteError(
                    f'Validator data for {arg} could not be found. Likely invalid validator.')

            try:
                valid_index = result['validatorindex']
                valid_pubkey = Eth2PubKey(result['pubkey'])
            except KeyError as e:
                msg = str(e)
                if isinstance(e, KeyError):
                    msg = f'Missing key entry for {msg}.'

                raise RemoteError(f'Failed to query beaconcha.in for validator data due to: {msg}') from e  # noqa: E501

        with self.database.user_write() as write_cursor:
            # by now we have a valid index and pubkey. Add to DB
            dbeth2.add_validators(write_cursor, [
                Eth2Validator(
                    index=valid_index,
                    public_key=valid_pubkey,
                    ownership_proportion=ownership_proportion,
                ),
            ])

    def combine_block_with_tx_events(self) -> None:
        """Get all mev reward block production events and combine them with the
        transaction events if they can be found"""
        with self.database.conn.read_ctx() as cursor:
            cursor.execute(
                'SELECT B_H.identifier, B_T.block_number, B_H.notes FROM evm_transactions B_T '
                'LEFT JOIN evm_events_info B_E '
                'ON B_T.tx_hash=B_E.tx_hash LEFT JOIN history_events B_H '
                'ON B_E.identifier=B_H.identifier WHERE '
                'B_H.asset="ETH" AND B_H.type="receive" AND B_H.subtype="none" '
                'AND B_T.block_number=('
                'SELECT A_S.is_exit_or_blocknumber '
                'FROM history_events A_H LEFT JOIN eth_staking_events_info A_S '
                'ON A_H.identifier=A_S.identifier WHERE A_H.subtype="mev reward" AND '
                'A_S.is_exit_or_blocknumber=B_T.block_number AND '
                'A_H.amount=B_H.amount AND A_H.location_label=B_H.location_label)',
            )
            result = cursor.fetchall()

        changes = []
        for entry in result:
            changes.append((
                EthBlockEvent.form_event_identifier(entry[1]),
                2,
                f'{entry[2]} as mev reward for block {entry[1]}',
                HistoryEventType.STAKING.serialize(),
                HistoryEventSubType.MEV_REWARD.serialize(),
                entry[0],
            ))
        with self.database.user_write() as write_cursor:
            write_cursor.executemany(
                'UPDATE history_events '
                'SET event_identifier=?, sequence_index=?, notes=?, type=?, subtype=?'
                'WHERE identifier=?',
                changes,
            )

    # -- Methods following the EthereumModule interface -- #
    def on_account_addition(self, address: ChecksumEvmAddress) -> None:
        """Just add validators to DB."""
        try:
            self.fetch_and_update_eth1_validator_data([address])
        except RemoteError as e:
            self.msg_aggregator.add_error(
                f'Did not manage to query beaconcha.in api for address {address} due to {str(e)}.'
                f' If you have Eth2 staked balances the final balance results may not be accurate',
            )

    def on_account_removal(self, address: ChecksumEvmAddress) -> None:
        pass

    def deactivate(self) -> None:
        with self.database.user_write() as write_cursor:
            self.database.delete_eth2_daily_stats(write_cursor)
