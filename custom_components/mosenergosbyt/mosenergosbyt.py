""" Basic Mosenergosbyt API interaction. """
import asyncio
import json
import logging
from _ast import arg
from datetime import datetime, date
from enum import IntEnum
from hashlib import md5
from typing import Optional, List, Dict, Union, Iterable, Any, Type, Set, TypeVar
from urllib import parse

import aiohttp
from cached_property import cached_property
from dateutil.relativedelta import relativedelta
from dateutil.tz import tz

_LOGGER = logging.getLogger(__name__)

IndicationsType = Union[Iterable[Optional[int]], Dict[int, int]]
PaymentsList = List[Dict[str, Union[str, int, datetime]]]
InvoiceData = Dict[str, Any]
InvoiceList = List[InvoiceData]
IndicationsList = List[Dict[str, Any]]
AnyDate = Union[date, datetime]

MOSCOW_TIMEZONE = tz.gettz('Europe/Moscow')
OFFSET_START_TO_END = relativedelta(months=1, seconds=-1)


T = TypeVar('T')
def all_subclasses(cls: T) -> Set[T]:
    """
    Recursively find all subclasses of a given class.
    Code source: https://stackoverflow.com/a/3862957
    :param cls: Parent class
    :return: Set of subclasses
    """
    return set(cls.__subclasses__()).union(
        [s for c in cls.__subclasses__() for s in all_subclasses(c)])


class DateUtil:
    @staticmethod
    def moscow_today(to_datetime: bool = False, timezone: Optional['tz'] = None) -> AnyDate:
        """
        Generate date object from Moscow time.
        :return:
        """
        now = datetime.now(tz=timezone or MOSCOW_TIMEZONE)
        return now if to_datetime else now.date()

    @staticmethod
    def month_start(to_datetime: bool = False, timezone: Optional['tz'] = None) -> AnyDate:
        month_start = DateUtil.moscow_today(True, timezone or MOSCOW_TIMEZONE).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return month_start if to_datetime else month_start.date()

    @staticmethod
    def month_end(to_datetime: bool = False, timezone: Optional['tz'] = None) -> AnyDate:
        month_end = DateUtil.month_start(True, timezone or MOSCOW_TIMEZONE) + relativedelta(months=1, seconds=-1)
        return month_end if to_datetime else month_end.date()

    @staticmethod
    def convert_date(value: Union[datetime, date], to: Type[Union[datetime, date]] = datetime,
                     _none_now: bool = True):
        return (to.today() if _none_now else None) if arg is None \
            else value if isinstance(arg, to) \
            else to.fromordinal(value.toordinal())

    @staticmethod
    def convert_date_arguments(*args: Union[datetime, date], _to: Type[Union[datetime, date]] = datetime,
                               _none_now=True, **kwargs: Union[datetime, date]):
        if args and kwargs:
            raise ValueError('Only one type of arguments (positional or keyword) can be provided')

        elif kwargs:
            return {kw: DateUtil.convert_date(v, _to, _none_now) for kw, v in kwargs.items()}

        return [DateUtil.convert_date(v, _to, _none_now) for v in args]


class _IntEnum(IntEnum):
    @classmethod
    def list(cls) -> List[int]:
        # noinspection PyTypeChecker
        return list(map(int, cls))


class ServiceType(_IntEnum):
    ELECTRICITY = 1
    ELECTRICITY_TKO = 4


class Provider(_IntEnum):
    MES = 1
    MES_TKO = 6


class API:
    """ Mosenergosbyt API class """
    AUTH_URL = "https://my.mosenergosbyt.ru/auth"
    REQUEST_URL = "https://my.mosenergosbyt.ru/gate_lkcomu"

    def __init__(self, username: str, password: str, cache: bool = True, timeout: int = 5):
        self.__username = username
        self.__password = password

        self._user_agent = (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
            'AppleWebKit/537.36 (KHTML, like Gecko)'
            'Chrome/76.0.3809.100 Safari/537.36'
        )

        self._session: Optional[aiohttp.ClientSession] = None
        self._session_id: Optional[str] = None
        self._id_profile = None
        self._token = None
        self._accounts = None
        self._cookie_jar = None
        self._logged_in_at = None

        self._timeout = aiohttp.ClientTimeout(total=timeout)

    async def request(self, action, query, post_fields: Optional[Dict] = None, method='POST',
                      get_fields: Optional[Dict] = None, fail_on_reauth: bool = False):
        if get_fields is None:
            get_fields = {}

        get_fields['action'] = action
        get_fields['query'] = query
        if self._session_id is not None:
            get_fields['session'] = self._session_id

        encoded_params = parse.urlencode(get_fields)
        request_url = self.REQUEST_URL + '?' + encoded_params
        response_text = None
        try:
            _LOGGER.debug('-> (%s) %s' % (encoded_params, post_fields))
            async with self._session.post(request_url, data=post_fields) as response:
                response_text = await response.text(encoding='utf-8')
                _LOGGER.debug('<- (%d) %s' % (response.status, response_text))
                data = json.loads(response_text)

        except asyncio.exceptions.TimeoutError as e:
            raise MosenergosbytException('Timeout error (interaction exceeded %d seconds)'
                                         % self._timeout.total) from None

        except OSError as e:
            raise MosenergosbytException('Request error') from e

        except json.JSONDecodeError as e:
            _LOGGER.debug('Response contents: %s' % response_text)
            raise MosenergosbytException('Response contains invalid JSON') from None

        if data['success'] is not None and data['success']:
            return data

        elif data['err_code'] == 201:
            if fail_on_reauth:
                raise MosenergosbytException('Request returned')
            await self.login()
            return await self.request(
                action, query, post_fields,
                method, get_fields,
                fail_on_reauth=True
            )
        else:
            raise MosenergosbytException('Unknown error')

    async def request_sql(self, query, post_fields: Optional[Dict] = None):
        return await self.request('sql', query, post_fields, 'POST')

    def _get_cookies(self):
        return ';'.join(self._cookie_jar)

    async def login(self):
        self._session = aiohttp.ClientSession(raise_for_status=True, timeout=self._timeout)

        # await self._session.get(self.AUTH_URL)

        data = await self.request('auth', 'login', {
            'login': self.__username,
            'psw': self.__password,
            'remember': True,
            'vl_device_info': json.dumps({
                'appver': '1.8.0',
                'type': 'browser',
                'userAgent': self._user_agent
            })
        })

        # @TODO: Multiple profiles possible?
        profile = data['data'][0]
        if profile['id_profile'] is None:
            raise MosenergosbytException(profile['nm_result'])

        self._id_profile = profile['id_profile']
        self._session_id = profile['session']
        self._token = profile['new_token']

        await self.request_sql('Init')
        await self.request_sql('NoticeRoutine')

        self._logged_in_at = datetime.utcnow()

        return True

    @property
    def is_logged_in(self) -> bool:
        return self._session and self._logged_in_at

    @property
    def logged_in_at(self) -> datetime:
        return self._logged_in_at

    async def logout(self):
        if self._session:
            await self._session.close()

        self._session = None
        self._id_profile = None
        self._token = None
        self._accounts = None
        self._cookie_jar = None
        self._logged_in_at = None

        return True

    async def get_accounts(self) -> Dict[str, '_BaseAccount']:
        response = await self.request_sql('LSList')

        providers = _BaseAccount.provider_classes().keys()
        # service_types_list = ServiceType.list()

        return {
            account['nn_ls']: _BaseAccount.get_instance(
                account_data=account,
                api=self
            )
            for account in response['data']
            if account['kd_provider'] in providers
        }


class _BaseAccount:
    """ Base account for implementing provider accounts. """
    provider_id = NotImplemented

    ACCOUNT_URL = "https://my.mosenergosbyt.ru/accounts"

    def __init__(self, account_data: Dict[str, Any], api: API):
        self._account_data = account_data
        self._meter_objects: Optional[Dict[str, _BaseMeter]] = None
        self.api = api

    @staticmethod
    def provider_classes() -> Dict[int, '_BaseAccount']:
        return {subclass.provider_id: subclass
                for subclass in all_subclasses(_BaseAccount)}

    @classmethod
    def get_instance(cls: Type['_BaseAccount'], account_data: Dict[str, Any], api: API) -> '_BaseAccount':
        instance_provider_id = account_data['kd_provider']

        if cls == _BaseAccount:
            create_class = cls.provider_classes().get(instance_provider_id)
            if create_class is None:
                raise UnsupportedProviderException('Unsupported provider (%d, "%s")'
                                                   % (instance_provider_id, account_data['nm_provider']))

            return create_class(account_data, api)

        if instance_provider_id != cls.provider_id:
            raise UnsupportedProviderException('Unsupported provider (%d, "%s")'
                                               % (instance_provider_id, account_data['nm_provider']))

        return cls(account_data, api)

    # Properties
    @property
    def meter_objects(self) -> Optional[Dict[str, '_BaseMeter']]:
        return self._meter_objects

    @property
    def service_id(self):
        return self._account_data['id_service']

    @property
    def account_url(self):
        return self.ACCOUNT_URL + '/' + self._account_data['id_service']

    @property
    def account_code(self):
        return self._account_data['nn_ls']

    @property
    def address(self):
        return self._account_data['data']['nm_street']

    @property
    def provider_name(self) -> str:
        return self._account_data['nm_provider']

    @property
    def service_type(self) -> ServiceType:
        return ServiceType(self._account_data['kd_service_type'])

    @property
    def service_name(self) -> str:
        return self._account_data['nm_type']

    @property
    def is_locked(self) -> bool:
        return self._account_data['kd_status'] > 1

    @property
    def lock_reason(self) -> Optional[str]:
        return self._account_data['nm_lock_msg']

    # Base methods
    async def proxy_request(self, plugin: str, proxy_query: str, data: Optional[Dict] = None):
        data = {} if data is None else {**data}
        data['vl_provider'] = self._account_data['vl_provider']
        data['proxyquery'] = proxy_query
        data['plugin'] = plugin

        return await self.api.request(
            action='sql',
            query=plugin,
            post_fields=data
        )

    # Shortcut methods
    async def get_indications(self, start: AnyDate, end: Optional[AnyDate] = None) -> IndicationsList:
        start, end = DateUtil.convert_date_arguments(start, end, _to=datetime, _none_now=True)
        return await self._get_indications(start, end)

    async def get_last_indications(self) -> Optional[Dict]:
        now = DateUtil.moscow_today(True)
        indications = await self._get_indications(now - relativedelta(months=1), now)
        return indications[0] if indications else None

    async def get_invoices(self, start: AnyDate, end: Optional[AnyDate] = None) -> List['Invoice']:
        start, end = DateUtil.convert_date_arguments(start, end, _to=datetime, _none_now=True)
        return await self._get_invoices(start, end)

    async def get_last_invoice(self) -> Optional['Invoice']:
        now = DateUtil.month_end(True)
        previous_month_start = DateUtil.month_start(True) - relativedelta(months=1)
        invoices = await self._get_invoices(previous_month_start, now)
        return invoices[0] if invoices else None

    async def get_last_payment(self) -> Optional[Dict]:
        payments = await self.get_latest_payments()
        if not payments:
            return None
        return payments[0]

    async def get_latest_payments(self, months: int = 3) -> PaymentsList:
        now = DateUtil.moscow_today(True)
        return await self.get_payments(now - relativedelta(months=months), now)

    async def get_payments(self, start: AnyDate, end: Optional[AnyDate] = None) -> PaymentsList:
        start, end = DateUtil.convert_date_arguments(start, end, _to=datetime)
        return await self._get_payments(start, end)

    # Methods to override
    async def get_current_balance(self) -> float:
        raise NotImplementedError

    async def _get_indications(self, start: datetime, end: datetime) -> List[Dict[str, Any]]:
        raise NotImplementedError

    async def _get_invoices(self, start: datetime, end: datetime) -> List['Invoice']:
        raise NotImplementedError

    async def get_meters(self) -> Dict[str, '_BaseMeter']:
        raise NotImplementedError

    async def _get_payments(self, start: datetime, end: datetime) -> PaymentsList:
        raise NotImplementedError

    async def get_remaining_days(self) -> Optional[int]:
        raise NotImplementedError


class MESAccount(_BaseAccount):
    """ Mosenergosbyt Account class """
    provider_id = 1

    # Connection
    async def lk_byt_proxy(self, proxy_query, data: Optional[Dict] = None):
        return await self.proxy_request('bytProxy', proxy_query, data)

    # Base implementation
    async def get_meters(self) -> Dict[str, 'MESElectricityMeter']:
        response = await self.lk_byt_proxy('Meters')

        if self._meter_objects is None:
            self._meter_objects = {
                meter_data['nm_meter_num']: MESElectricityMeter(account=self, data=meter_data)
                for meter_data in response['data']
            }
            return self._meter_objects

        keep_meter_objects = set()

        for meter_data in response['data']:
            meter_code = meter_data['nm_meter_num']
            if meter_code in self._meter_objects:
                meter = self._meter_objects[meter_code]
                meter.data = meter_data
                keep_meter_objects.add(meter_code)
            else:
                meter = MESElectricityMeter(account=self, data=meter_data)
                self._meter_objects[meter_code] = meter
                keep_meter_objects.add(meter_code)

        for meter_code in self._meter_objects.keys() - keep_meter_objects:
            del self._meter_objects[meter_code]

        return self._meter_objects

    async def _get_invoices(self, start: datetime, end: datetime) -> List['Invoice']:
        response = await self.lk_byt_proxy('Invoice', {
            'dt_st': start.isoformat(),
            'dt_en': end.isoformat(),
        })

        invoices = []
        for invoice in response['data']:
            charges = {}
            for i, charge in enumerate(invoice['data_detail'], start=1):
                charge_part_iter = iter(charge)

                charge_dict = {}

                try:
                    head_part = next(charge_part_iter)
                    charge_dict['name'] = head_part['nm_value']
                    charge_dict['total'] = head_part['vl_value']

                    value_part = next(charge_part_iter)
                    charge_dict['unit'] = value_part['nm_mu']
                    charge_dict['value'] = value_part['vl_value']

                    tariff_part = next(charge_part_iter)
                    charge_dict['cost'] = tariff_part['vl_value']

                except StopIteration:
                    raise MosenergosbytException('Invoice (%s) data incomplete' % invoice['id_korr']) from invoice

                charges['t%d' % i] = charge_dict

            calculations = {}
            for section in invoice['data_common']:
                section_name = section['nm_value']
                value = section['vl_value']
                if section_name.startswith('Итого'):
                    calculations[Invoice.TOTAL] = value
                elif 'начислено' in section_name:
                    calculations[Invoice.COSTS.CHARGED] = value
                elif 'долж' in section_name:
                    calculations[Invoice.INITIAL_BALANCE] = value
                elif 'поступ' in section_name:
                    calculations[Invoice.DEDUCTIONS.PAYMENTS] = value

            invoices.append(Invoice(
                account=self,
                invoice_id=str(invoice['id_korr']),
                period=datetime.fromisoformat(invoice['dt_period']).date(),
                calculations=calculations,
                charges=charges))

        return invoices

    async def _get_payments(self, start: datetime, end: datetime) -> PaymentsList:
        response = await self.lk_byt_proxy('Pays', {
            'dt_st': start.isoformat(),
            'dt_en': end.isoformat()
        })

        return [{
            'date': datetime.fromisoformat(payment['dt_pay']),
            'amount': payment['sm_pay'],
            'status': payment['nm_status'],
        } for payment in response['data']]

    async def get_current_balance(self) -> float:
        response = await self.lk_byt_proxy('CurrentBalance')
        return response['data'][0]['vl_balance']

    async def get_remaining_days(self) -> int:
        response = await self.lk_byt_proxy('IndicationCounter')
        return max(0, response['data'][0]['nn_days'])

    async def _get_indications(self, start: datetime, end: datetime) -> IndicationsList:
        response = await self.lk_byt_proxy('Indications', {
            'dt_st': start.isoformat(),
            'dt_en': end.isoformat()
        })

        return [{
            'date': indication['dt_indication'],
            '_taken_by': indication['nm_take'],
            '_source': indication['nm_indication_take'],
            'meters': {
                k[-2:]: v
                for k, v in indication.items()
                if k[:-1] == 'vl_t'
            }
        } for indication in response['data']]


class MESTKOAccount(_BaseAccount):
    """ Mosenergosbyt + TKO account class """
    provider_id = 6

    # Connection
    async def lk_trash_proxy(self, proxy_query: str, data: Optional[Dict] = None):
        return await self.proxy_request('trashProxy', proxy_query, data)

    # Helper methods
    @staticmethod
    def _generate_indication_id(name: str):
        lower_name = name.lower()
        if 'тко' in lower_name:
            return 'tko'
        elif 'ночь' in lower_name:
            return 'night'
        elif 'день' in lower_name:
            return 'day'
        return md5(lower_name.encode('utf-8')).hexdigest()[:6]

    @staticmethod
    def _generate_indications_from_charges(charges: List[Dict[str, Any]], with_calculations: bool = False) \
            -> Dict[str, Dict[str, Union[float, Dict[str, float]]]]:
        indications = {}
        for charge in charges:
            charge_dict = {
                Invoice.ATTRS.NAME: charge['nm_service'],
                Invoice.ATTRS.UNIT: charge['nm_measure_unit'],
                Invoice.ATTRS.VALUE: charge['vl_charged_volume'],
                Invoice.ATTRS.COST: charge['vl_tariff'],
            }

            if with_calculations:
                charge_dict[Invoice.ATTRS.CALCULATIONS] = {
                    Invoice.ADJUSTMENTS: charge['sm_recalculations'],
                    Invoice.INITIAL_BALANCE: charge['sm_start'],
                    Invoice.COSTS.CHARGED: charge['sm_charged'],
                    Invoice.COSTS.PENALTY: charge['sm_penalty'],
                    Invoice.DEDUCTIONS.BENEFITS: charge['sm_benefits'],
                    Invoice.DEDUCTIONS.PAYMENTS: charge['sm_payed'],
                    Invoice.TOTAL: charge['sm_total'],
                }

            indications[MESTKOAccount._generate_indication_id(charge['nm_service'])] = charge_dict

        return indications

    # Base implementation
    async def _get_invoices(self, start: datetime, end: datetime) -> List['Invoice']:
        response = await self.lk_trash_proxy('AbonentChargeDetail', {
            'dt_period_start': start.isoformat(),
            'dt_period_end': end.isoformat(),
            'kd_tp_mode': 1
        })

        return [Invoice(
            account=self,
            invoice_id=invoice['vl_report_uuid'],
            period=datetime.fromisoformat(invoice_group['dt_period']).date(),
            calculations={
                Invoice.INITIAL_BALANCE: invoice['sm_start'],
                Invoice.ADJUSTMENTS: invoice['sm_recalculations'],
                Invoice.COSTS.CHARGED: invoice['sm_charged'],
                Invoice.COSTS.INSURANCE: invoice['sm_insurance'],
                Invoice.COSTS.PENALTY: invoice['sm_penalty'],
                Invoice.DEDUCTIONS.BENEFITS: invoice['sm_benefits'],
                Invoice.DEDUCTIONS.PAYMENTS: invoice['sm_payed'],
                Invoice.TOTAL: invoice['sm_total'],
            },
            charges=self._generate_indications_from_charges(invoice['child'], with_calculations=True))
            for invoice_group in response['data'] for invoice in invoice_group['child']]

    async def _get_payments(self, start: datetime, end: datetime) -> PaymentsList:
        response = await self.lk_trash_proxy('AbonentPays', {
            'dt_st': start.isoformat(),
            'dt_en': end.isoformat()
        })

        return [{
            'date': datetime.fromisoformat(payment['dt_pay']),
            'amount': payment['sm_pay'],
            'status': payment['nm_pay_state'],
        } for payment in response['data']]

    async def _get_indications(self, start: datetime, end: datetime) -> IndicationsList:
        response = await self.lk_trash_proxy('AbonentChargeDetail', {
            'dt_period_start': start.isoformat(),
            'dt_period_end': end.isoformat(),
            'kd_tp_mode': 1
        })

        return [
            self._generate_indications_from_charges(invoice['child'], with_calculations=False)
            for invoice_group in response['data'] for invoice in invoice_group['child']
        ]

    async def get_meters(self) -> Dict[str, 'TKOIndicationMeter']:
        indications = await self.get_last_indications()

        if self._meter_objects is None:
            meter_objects = {self.account_code: TKOIndicationMeter(self, indications)}
            self._meter_objects = meter_objects
            return meter_objects

        self._meter_objects[self.account_code].data = indications
        return self._meter_objects

    async def get_current_balance(self) -> float:
        response = await self.lk_trash_proxy('AbonentCurrentBalance')
        return response['data'][0]['sm_balance']

    async def get_remaining_days(self) -> Optional[int]:
        # @TODO: approximate using bill data
        return None

    # Additional overrides
    async def get_last_indications(self) -> Optional[Dict]:
        now = DateUtil.moscow_today(True)
        previous_month_start = DateUtil.month_start(True) - relativedelta(months=1)
        indications = await self._get_indications(previous_month_start, now)
        return indications[0] if indications else None


class _BaseMeter:
    DataType = TypeVar('DataType')

    def __init__(self, account: '_BaseAccount', data: DataType):
        """
        Initializes Meter object
        :param account: Tied account
        :param data: Meter data
        """
        self._data = data
        self._account = account

    def __str__(self):
        return '%s[%s]' % (type(self).__name__, self.meter_code)

    def __repr__(self):
        return '<%s>' % self

    @property
    def data(self) -> DataType:
        return self._data

    @data.setter
    def data(self, value: DataType):
        self._data = value

    @property
    def account_code(self) -> str:
        """
        Parent account code accessor.
        :return:
        """
        return self._account.account_code

    # Override in inherent classes
    @cached_property
    def meter_code(self) -> str:
        raise NotImplementedError

    @cached_property
    def indications_units(self) -> List[str]:
        raise NotImplementedError

    @cached_property
    def indications_names(self) -> List[str]:
        raise NotImplementedError

    @cached_property
    def indications_ids(self) -> List[str]:
        raise NotImplementedError

    @cached_property
    def last_indications_dict(self) -> Dict[str, Dict[str, Union[float, str]]]:
        return {a: {'value': b, 'unit': c, 'name': d} for a, b, c, d in zip(
            self.indications_ids,
            self.last_indications,
            self.indications_units,
            self.indications_names
        )}

    @cached_property
    def last_indications(self) -> List[float]:
        raise NotImplementedError

    @cached_property
    def period_start_date(self) -> date:
        raise NotImplementedError

    @cached_property
    def period_end_date(self) -> date:
        raise NotImplementedError

    @cached_property
    def remaining_submit_days(self) -> Optional[int]:
        """
        Calculate remaining days to submit indications
        :return:
        """
        raise NotImplementedError

    @cached_property
    def current_status(self) -> Optional[str]:
        raise NotImplementedError


class MESElectricityMeter(_BaseMeter):
    """ Mosenergosbyt meter class """

    @cached_property
    def meter_code(self) -> str:
        return self._data['nm_meter_num']

    @cached_property
    def indications_units(self) -> List[str]:
        return ['кВт/ч']*self.tariff_count

    @cached_property
    def indications_names(self) -> List[str]:
        return [self._data['nm_t%d' % i] for i in range(1, self.tariff_count + 1)]

    @cached_property
    def indications_ids(self) -> List[str]:
        return ['t%d' % i for i in range(0, self.tariff_count)]

    @cached_property
    def install_date(self) -> date:
        """
        Meter install date accessor.
        :return: Installation date object
        """
        return datetime.fromisoformat(self._data['dt_meter_install']).date()

    @cached_property
    def model(self) -> str:
        """
        Meter model shortcut accessor.
        :return: Meter model string
        """
        return self._data['nm_mrk']

    @cached_property
    def tariff_names(self) -> Dict[int, str]:
        return {
            k: v
            for k, v in self._data.items()
            if k[:-1] == 'nm_t'
        }

    @cached_property
    def tariff_count(self) -> int:
        """
        Tariff count accessor.
        Skims meter data for existing indications, then deduces tariff count based
        on indication values.
        :return:
        """
        return len(self.tariff_names)

    @cached_property
    def submitted_indications(self) -> List[float]:
        """
        Submitted indications accessor.
        Skims meter data for submitted indications and prepares data.
        :return:
        """
        return [self._data['vl_t%d_today' % i] for i in range(1, self.tariff_count + 1)]

    @cached_property
    def last_indications(self) -> List[float]:
        """
        Last indications accessor.
        Skims meter data for submitted indications and prepares data.
        :return:
        """
        return [self._data['vl_t%d_last_ind' % i] for i in range(1, self.tariff_count + 1)]

    @property
    def period_start_date(self) -> date:
        return DateUtil.moscow_today().replace(day=self._data['nn_period_start'])

    @property
    def period_end_date(self) -> date:
        return DateUtil.moscow_today().replace(day=self._data['nn_period_end'])

    @property
    def remaining_submit_days(self):
        """
        Calculate remaining days to submit indications
        :return:
        """
        return (self.period_end_date - DateUtil.moscow_today(False)).days + 1

    @cached_property
    def current_status(self):
        return self._data['nm_result']

    # Submission algorithms
    @staticmethod
    def _get_indications_vl_params(indications: IndicationsType) -> Dict[str, int]:
        """
        Converts indications input to format suitable for sending over HTTP.
        :param indications: Indications data
        :return: Converted indications
        """
        if isinstance(indications, dict):
            iterator = indications.values()
        else:
            iterator = enumerate(indications, start=1)

        return {
            'vl_t%d' % tariff_id: indication
            for tariff_id, indication in iterator
            if indication is not None
        }

    def check_submission_date(self, submission_date: Optional[date] = None) -> None:
        """
        Checks whether submission date is within bounds.
        :param submission_date: (optional) Submission date object (checks today in Moscow TZ by default)
        :raises MosenergosbytException: Submission date is out of current period
        :return:
        """
        if submission_date is None:
            now = datetime.now(tz=tz.gettz('Europe/Moscow'))
            submission_date = now.date()

        if not (self.period_start_date <= submission_date <= self.period_end_date):
            raise MosenergosbytException('Out of period (from %s to %s) submission date (%s)'
                                         % (self.period_start_date, self.period_end_date, submission_date))

    def check_indication_params(self, params: Dict[str, int]) -> None:
        """
        Checks indication parameters.
        This must be a new array acquired via `_get_indications_vl_params` (without any extra keys but indications
        added to it). Otherwise, the method will raise an `IndicationsCountException` on possibly correct data.

        :param params: Indications (ex. {'vl_t1': 123})
        :raises IndicationsCountException: There is a number of indications differing from meter tariff count
        :raises IndicationsThresholdException: New indications are below submitted / previous indications threshold
        """
        if len(params) != self.tariff_count:
            raise IndicationsCountException('Indications count (%d) is not equal to meter tariff count (%d)'
                                            % (len(params), self.tariff_count))

        for parameter, new_value in params.items():
            for suffix, name in [('_last_ind', 'previous'), ('_today', 'submitted')]:
                old_value = self._data.get(parameter + suffix, new_value)
                if new_value < old_value:
                    raise IndicationsThresholdException('Indication T%s (%d) is lower than %s indication (%d)'
                                                        % (parameter[-1], new_value, name, old_value))

    async def save_indications(self, indications: IndicationsType, ignore_period_check: bool = False,
                               ignore_indications_check: bool = False) -> None:
        """
        Submit indications to Mosenergosbyt.
        Use this method with caution! There are safeguards in place to prevent an out-of-period and incomplete
        submissions. Override these safeguards at your own risk.
        :param indications: Indications list / dictionary
        :param ignore_period_check: Ignore out-of-period safeguard
        :param ignore_indications_check: Ignore indications miscount or lower-than-threshold safeguard
        """
        if not ignore_period_check:
            self.check_submission_date()

        request_dict = self._get_indications_vl_params(indications)

        if not ignore_indications_check:
            self.check_indication_params(request_dict)

        request_dict['pr_flat_meter'] = self._data['pr_flat_meter']

        await self._account.lk_byt_proxy('SaveIndications', request_dict)

    # async def get_charge_indications(self, indications: IndicationsType):
    #     request_dict = self._get_indications_vl_params(indications)
    #     request_dict['pr_flat_meter'] = self._data['pr_flat_meter']
    #
    #     response = await self._account.lk_byt_proxy('CalcCharge', request_dict)


class TKOIndicationMeter(_BaseMeter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._meter_code = self.account_code

    @property
    def indications_units(self) -> List[str]:
        return [a['unit'] for a in self._data.values()]

    @property
    def indications_names(self) -> List[str]:
        return [a['name'] for a in self._data.values()]

    @property
    def indications_ids(self) -> List[str]:
        return list(self._data.keys())

    @property
    def last_indications_dict(self) -> Dict[str, Dict[str, Union[float, str]]]:
        return self._data

    @property
    def meter_code(self) -> str:
        return self._meter_code

    @property
    def last_indications(self) -> List[float]:
        return [a['value'] for a in self._data.values()]

    @property
    def period_start_date(self) -> date:
        return DateUtil.month_start()

    @property
    def period_end_date(self) -> date:
        return (DateUtil.month_start(True) + relativedelta(months=1, days=-1)).date()

    @property
    def remaining_submit_days(self) -> Optional[int]:
        return None

    @property
    def current_status(self) -> Optional[str]:
        return None

    # Meter-specific properties
    @property
    def tariff_price(self) -> float:
        return self._data['cost']

    @property
    def total_cost(self) -> float:
        return self._data['total']


class Invoice:
    INITIAL_BALANCE = 'initial_balance'
    ADJUSTMENTS = 'adjustments'
    TOTAL = 'total'

    class ATTRS:
        NAME = 'name'
        UNIT = 'unit'
        VALUE = 'value'
        COST = 'cost'
        CALCULATIONS = 'calculations'

    class COSTS:
        CHARGED = 'charged'
        INSURANCE = 'insurance'
        PENALTY = 'penalty'

    class DEDUCTIONS:
        BENEFITS = 'benefits'
        PAYMENTS = 'payments'

    def __init__(self, account: '_BaseAccount', invoice_id: str, period: date, charges: Dict[str, Dict[str, Any]],
                 calculations: Optional[Dict[str, float]] = None):
        self._account = account
        self._invoice_id = invoice_id
        self._period = period
        self._charges = charges
        self._calculations = calculations

    def _attribute_from_calculations(self, attribute: str) -> float:
        if self._calculations is not None and attribute in self._calculations:
            return self._calculations[attribute]
        return sum([c.get(self.ATTRS.CALCULATIONS, {}).get(attribute, 0) for c in self._charges.values()])

    @property
    def account(self) -> '_BaseAccount':
        return self._account

    @property
    def invoice_id(self) -> str:
        return self._invoice_id

    @property
    def period(self) -> date:
        return self._period

    @cached_property
    def total(self) -> float:
        return self._attribute_from_calculations(self.TOTAL)

    @cached_property
    def charged(self) -> float:
        return self._attribute_from_calculations(self.COSTS.CHARGED)

    @cached_property
    def initial_balance(self) -> float:
        return self._attribute_from_calculations(self.INITIAL_BALANCE)

    @cached_property
    def paid_amount(self) -> float:
        return self._attribute_from_calculations(self.DEDUCTIONS.PAYMENTS)

    @cached_property
    def insurance(self) -> float:
        return 0 if self._calculations is None else self._calculations.get(self.COSTS.INSURANCE, 0)

    @cached_property
    def benefits(self) -> float:
        return self._attribute_from_calculations(self.DEDUCTIONS.BENEFITS)

    @cached_property
    def penalty(self) -> float:
        return self._attribute_from_calculations(self.COSTS.PENALTY)


class MosenergosbytException(Exception):
    pass


class SubmissionPeriodException(MosenergosbytException):
    pass


class IndicationsCountException(MosenergosbytException):
    pass


class IndicationsThresholdException(MosenergosbytException):
    pass


class UnsupportedServiceException(MosenergosbytException):
    pass


class UnsupportedProviderException(MosenergosbytException):
    pass
