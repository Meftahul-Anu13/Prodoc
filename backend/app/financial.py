"""Conservative source-backed financial policy, separate from the legacy benchmark."""
import re
from decimal import Decimal, InvalidOperation, localcontext

POLICY_VERSION = 'source-backed-v2'
# Explicit supported subset, not a claim to implement the whole ISO registry.
MINOR_UNITS = {code: 2 for code in (
    'USD EUR GBP CAD AUD NZD CHF CNY HKD SGD INR BDT PKR LKR NPR AED SAR QAR '
    'ILS ZAR BRL MXN ARS CLP COP PEN SEK NOK DKK PLN CZK HUF RON TRY THB MYR IDR PHP EGP').split()}
MINOR_UNITS.update({code: 0 for code in 'JPY KRW VND CLP XAF XOF XPF'.split()})
MINOR_UNITS.update({code: 3 for code in 'BHD IQD JOD KWD LYD OMR TND'.split()})


def number(value):
    if not isinstance(value, str) or not re.fullmatch(r'[+-]?\d+(?:\.\d+)?', value) or len(value) > 80:
        raise ValueError('canonical_decimal_string_required')
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError('invalid_decimal') from exc
    if not result.is_finite() or len(result.as_tuple().digits) > 28 or abs(result.as_tuple().exponent) > 20:
        raise ValueError('decimal_precision_exceeded')
    return result


def validate_source_invoice(invoice):
    from .pipeline import _unambiguous_date
    findings = []

    def finding(rule, message, severity='error', **detail):
        findings.append({'rule_id': rule, 'message': message, 'severity': severity,
                         'rule_version': POLICY_VERSION, **detail})

    for field in ('invoice_number', 'invoice_date', 'supplier_name', 'customer_name', 'currency', 'subtotal', 'tax', 'total'):
        if invoice.get(field) in (None, ''):
            finding('missing_field', f'Missing {field}; supply a reviewed correction.', field=field)
    date = invoice.get('invoice_date')
    if date and _unambiguous_date(date) is None:
        finding('invalid_or_ambiguous_date', 'Date needs an unambiguous calendar value.', field='invoice_date')
    currency = invoice.get('currency')
    scale = MINOR_UNITS.get(currency)
    tolerance = Decimal(1).scaleb(-scale) if scale is not None else None
    if scale is None:
        finding('unsupported_currency', 'Currency is missing or outside this policy\'s explicit supported subset.', field='currency')
    parsed = {}
    for field in ('subtotal', 'tax', 'discount', 'other_charges', 'total'):
        value = invoice.get(field)
        if value is None:
            continue
        try:
            parsed[field] = number(value)
            if scale is not None and parsed[field].as_tuple().exponent < -scale:
                with localcontext() as context:
                    context.prec = 80
                    if parsed[field].quantize(tolerance) != parsed[field]:
                        finding('currency_precision', f'{field} exceeds the currency minor-unit scale.', field=field, minor_units=scale)
        except ValueError:
            finding('invalid_numeric', f'{field} must be finite canonical Decimal text.', field=field)
    required = {'subtotal', 'tax', 'discount', 'other_charges', 'total'}
    if tolerance is None or not required <= parsed.keys():
        finding('financial_terms_unverified', 'Header math requires explicit currency, subtotal, tax, discount, other charges and total. Missing adjustments are not zero.', fields=sorted(required - parsed.keys()))
    else:
        with localcontext() as context:
            context.prec = 80
            difference = parsed['subtotal'] + parsed['tax'] - parsed['discount'] + parsed['other_charges'] - parsed['total']
        if abs(difference) > tolerance:
            finding('total_mismatch', 'Subtotal + tax - discount + other charges differs from total.', difference=str(difference), tolerance=str(tolerance))
    items = invoice.get('line_items')
    if not isinstance(items, list):
        finding('invalid_line_items', 'Line items must be a list.')
        return findings
    if not items:
        finding('missing_line_items', 'No extracted line items; confirm against the original.', 'needs_review')
    basis = invoice.get('line_amount_basis')
    if items and basis not in {'net', 'gross'}:
        finding('line_tax_basis_unknown', 'Confirm line_amount_basis as net or gross before financial acceptance.')
    line_totals = []
    line_taxes = []
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict):
            finding('invalid_line_item', 'Line item must be an object.', line=index)
            continue
        values = {}
        for field in ('quantity', 'unit_price', 'discount', 'tax', 'line_total'):
            if item.get(field) is None:
                continue
            try:
                values[field] = number(item[field])
            except ValueError:
                finding('invalid_numeric', f'Line {index}: invalid {field}.', line=index, field=field)
        if 'line_total' in values:
            line_totals.append(values['line_total'])
        if 'tax' in values:
            line_taxes.append(values['tax'])
        needed = {'quantity', 'unit_price', 'discount', 'line_total'} | ({'tax'} if basis == 'gross' else set())
        if basis not in {'net', 'gross'} or tolerance is None or not needed <= values.keys():
            finding('line_terms_unverified', 'Line math needs an explicit basis and monetary terms; absent line discounts/taxes are not zero.', 'needs_review', line=index)
        else:
            with localcontext() as context:
                context.prec = 100
                delta = values['quantity'] * values['unit_price'] - values['discount'] - values['line_total']
                if basis == 'gross':
                    delta += values['tax']
            if abs(delta) > tolerance:
                finding('line_item_mismatch', 'Line extended amount differs from quantity × price minus discount (+ tax for gross).', line=index, difference=str(delta), tolerance=str(tolerance))
    if items and tolerance is not None and len(line_totals) == len(items) and 'subtotal' in parsed:
        with localcontext() as context:
            context.prec = 100
            net = sum(line_totals, Decimal(0))
            if basis == 'gross' and len(line_taxes) == len(items):
                net -= sum(line_taxes, Decimal(0))
            if basis == 'net' or (basis == 'gross' and len(line_taxes) == len(items)):
                delta = net - parsed['subtotal']
                if abs(delta) > tolerance:
                    finding('line_items_subtotal_mismatch', 'Line net amounts differ from subtotal; adjustment scope needs review.', difference=str(delta), tolerance=str(tolerance))
    if items and tolerance is not None:
        if len(line_taxes) == len(items) and 'tax' in parsed:
            with localcontext() as context:
                context.prec = 100
                delta = sum(line_taxes, Decimal(0)) - parsed['tax']
            if abs(delta) > tolerance:
                finding('line_items_tax_mismatch', 'Line taxes differ from header tax; tax/adjustment scope needs review.', difference=str(delta), tolerance=str(tolerance))
        elif 'tax' in parsed:
            finding('line_taxes_unverified', 'Some line taxes are absent; header-to-line tax reconciliation is unverified.', 'needs_review')
    return findings
