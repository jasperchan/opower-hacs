"""Coordinator to handle Opower connections."""

from datetime import datetime, timedelta
import logging
from types import MappingProxyType
from typing import Any, cast

from opower_gql import (
    Account,
    AggregateType,
    CostRead,
    Forecast,
    InvalidAuth,
    MeterType,
    Opower,
)

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import CONF_UTILITY, DOMAIN

_LOGGER = logging.getLogger(__name__)


class OpowerCoordinator(DataUpdateCoordinator[dict[str, Forecast]]):
    """Handle fetching Opower data, updating sensors and inserting statistics."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_data: MappingProxyType[str, Any],
    ) -> None:
        """Initialize the data handler."""
        super().__init__(
            hass,
            _LOGGER,
            name="Opower",
            # Data is updated daily on Opower.
            # Refresh every 12h to be at most 12h behind.
            update_interval=timedelta(hours=12),
        )
        self.api = Opower(
            aiohttp_client.async_get_clientsession(hass),
            entry_data[CONF_UTILITY],
            entry_data[CONF_USERNAME],
            entry_data[CONF_PASSWORD],
        )

    async def _async_update_data(
        self,
    ) -> dict[str, Forecast]:
        """Fetch data from API endpoint."""
        try:
            # Login expires after a few minutes.
            # Given the infrequent updating (every 12h)
            # assume previous session has expired and re-login.
            await self.api.async_login()
        except InvalidAuth as err:
            raise ConfigEntryAuthFailed from err
        accounts: list[Account] = await self.api.async_get_accounts()
        await self._insert_statistics(accounts)
        # Don't return anything since we want to use the real opower integration for forecasts
        return {}

    async def _insert_statistics(self, accounts: list[Account]) -> None:
        """Insert Opower statistics."""
        for account in accounts:
            id_prefix = "_".join(
                (
                    self.api.utility.subdomain(),
                    account.meter_type.name.lower(),
                    account.utility_account_id,
                )
            )
            cost_statistic_id = f"{DOMAIN}:{id_prefix}_energy_cost"
            consumption_statistic_id = f"{DOMAIN}:{id_prefix}_energy_consumption"
            received_statistic_id = f"{DOMAIN}:{id_prefix}_energy_received"
            delivered_statistic_id = f"{DOMAIN}:{id_prefix}_energy_delivered"
            _LOGGER.debug(
                "Updating Statistics for %s, %s, %s, %s",
                cost_statistic_id,
                consumption_statistic_id,
                received_statistic_id,
                delivered_statistic_id,
            )

            last_stat = await get_instance(self.hass).async_add_executor_job(
                get_last_statistics, self.hass, 1, consumption_statistic_id, True, set()
            )
            if not last_stat:
                _LOGGER.debug("Updating statistic for the first time")
                cost_reads = await self._async_get_all_cost_reads(account)
                cost_sum = 0.0
                consumption_sum = 0.0
                received_sum = 0.0
                delivered_sum = 0.0
                last_stats_time = None
            else:
                cost_reads = await self._async_get_recent_cost_reads(
                    account, last_stat[consumption_statistic_id][0]["start"]
                )
                if not cost_reads:
                    _LOGGER.debug("No recent usage/cost data. Skipping update")
                    continue
                stats = await get_instance(self.hass).async_add_executor_job(
                    statistics_during_period,
                    self.hass,
                    cost_reads[0].start_time,
                    None,
                    {
                        cost_statistic_id,
                        consumption_statistic_id,
                        received_statistic_id,
                        delivered_statistic_id,
                    },
                    "hour" if account.meter_type == MeterType.ELEC else "day",
                    None,
                    {"sum"},
                )
                cost_sum = cast(float, stats[cost_statistic_id][0]["sum"])
                consumption_sum = cast(float, stats[consumption_statistic_id][0]["sum"])
                received_sum = cast(float, stats[received_statistic_id][0]["sum"])
                delivered_sum = cast(float, stats[delivered_statistic_id][0]["sum"])
                last_stats_time = stats[cost_statistic_id][0]["start"]

            cost_statistics = []
            consumption_statistics = []
            received_statistics = []
            delivered_statistics = []

            for cost_read in cost_reads:
                start = cost_read.start_time
                if last_stats_time is not None and start.timestamp() <= last_stats_time:
                    continue
                cost_sum += cost_read.provided_cost
                consumption_sum += cost_read.consumption
                received_sum += cost_read.received
                delivered_sum += cost_read.delivered

                cost_statistics.append(
                    StatisticData(
                        start=start, state=cost_read.provided_cost, sum=cost_sum
                    )
                )
                consumption_statistics.append(
                    StatisticData(
                        start=start, state=cost_read.consumption, sum=consumption_sum
                    )
                )
                received_statistics.append(
                    StatisticData(
                        start=start, state=cost_read.received, sum=received_sum
                    )
                )
                delivered_statistics.append(
                    StatisticData(
                        start=start, state=cost_read.delivered, sum=delivered_sum
                    )
                )

            name_prefix = " ".join(
                (
                    "Opower",
                    self.api.utility.subdomain(),
                    account.meter_type.name.lower(),
                    account.utility_account_id,
                )
            )
            cost_metadata = StatisticMetaData(
                has_mean=False,
                has_sum=True,
                name=f"{name_prefix} cost",
                source=DOMAIN,
                statistic_id=cost_statistic_id,
                unit_of_measurement=None,
            )
            consumption_metadata = StatisticMetaData(
                has_mean=False,
                has_sum=True,
                name=f"{name_prefix} consumption",
                source=DOMAIN,
                statistic_id=consumption_statistic_id,
                unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR
                if account.meter_type == MeterType.ELEC
                else UnitOfVolume.CENTUM_CUBIC_FEET,
            )
            received_metadata = StatisticMetaData(
                has_mean=False,
                has_sum=True,
                name=f"{name_prefix} received",
                source=DOMAIN,
                statistic_id=received_statistic_id,
                unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR
                if account.meter_type == MeterType.ELEC
                else UnitOfVolume.CENTUM_CUBIC_FEET,
            )
            delivered_metadata = StatisticMetaData(
                has_mean=False,
                has_sum=True,
                name=f"{name_prefix} delivered",
                source=DOMAIN,
                statistic_id=delivered_statistic_id,
                unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR
                if account.meter_type == MeterType.ELEC
                else UnitOfVolume.CENTUM_CUBIC_FEET,
            )

            async_add_external_statistics(self.hass, cost_metadata, cost_statistics)
            async_add_external_statistics(
                self.hass, consumption_metadata, consumption_statistics
            )
            async_add_external_statistics(
                self.hass, received_metadata, received_statistics
            )
            async_add_external_statistics(
                self.hass, delivered_metadata, delivered_statistics
            )

    async def _async_get_all_cost_reads(self, account: Account) -> list[CostRead]:
        """Get all cost reads since account activation but at different resolutions depending on age.

        - month resolution for all years (since account activation)
        - day resolution for past 3 years
        - hour resolution for past 12 months, only for electricity, not gas
        """
        cost_reads = []
        cost_reads += await self.api.async_get_usage_reads(
            account,
            AggregateType.DAY,
            start_date=datetime.now() - timedelta(days=365 * 3),
            end_date=datetime.now(),
        )
        if account.meter_type == MeterType.ELEC:
            cost_reads += await self.api.async_get_usage_reads(
                account,
                AggregateType.HOUR,
                start_date=datetime.now() - timedelta(days=365),
                end_date=datetime.now(),
            )
        return cost_reads

    async def _async_get_recent_cost_reads(
        self, account: Account, last_stat_time: float
    ) -> list[CostRead]:
        """Get cost reads within the past 30 days to allow corrections in data from utilities.

        Hourly for electricity, daily for gas.
        """
        return await self.api.async_get_usage_reads(
            account,
            AggregateType.HOUR
            if account.meter_type == MeterType.ELEC
            else AggregateType.DAY,
            start_date=datetime.fromtimestamp(last_stat_time) - timedelta(days=30),
            end_date=datetime.now(),
        )
