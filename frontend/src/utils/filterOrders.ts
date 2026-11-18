import { type PublicOrder, type Favorites, type Federation } from '../models';
import thirdParties from '../../static/thirdparties.json';

interface AmountFilter {
  amount: string | number | null;
  minAmount: string | number | null;
  maxAmount: string | number | null;
  threshold: number;
}

interface FilterOrders {
  federation: Federation;
  baseFilter: Favorites;
  premium?: number | null;
  amountFilter?: AmountFilter | null;
  paymentMethods?: string[];
  /** Orders to filter instead of the federation book (Roblosats: book + onchain orders). */
  orders?: Array<PublicOrder | null | undefined>;
}

const filterByPayment = function (order: PublicOrder, paymentMethods: string[]): boolean {
  if (paymentMethods.length === 0) {
    return true;
  } else {
    let result = false;
    paymentMethods.forEach((method) => {
      result = result || order.payment_method.includes(method);
    });
    return result;
  }
};

const filterByHost = function (
  order: PublicOrder,
  shortAlias: string,
  federation: Federation,
): boolean {
  if (shortAlias === 'any') {
    return true;
  } else if (shortAlias === 'robosats') {
    const coordinator = federation.getCoordinator(order.coordinatorShortAlias ?? '');
    return coordinator?.federated ?? false;
  } else {
    return order.coordinatorShortAlias === shortAlias;
  }
};

const filterByAmount = function (order: PublicOrder, filter: AmountFilter): boolean {
  const filterMaxAmount =
    Number(filter.amount !== '' ? filter.amount : filter.maxAmount) * (1 + filter.threshold);
  const filterMinAmount =
    Number(filter.amount !== '' ? filter.amount : filter.minAmount) * (1 - filter.threshold);

  const orderMinAmount = Number(
    order.amount === '' || order.amount === null ? order.min_amount : order.amount,
  );
  const orderMaxAmount = Number(
    order.amount === '' || order.amount === null ? order.max_amount : order.amount,
  );

  return Math.max(filterMinAmount, orderMinAmount) <= Math.min(filterMaxAmount, orderMaxAmount);
};

const filterByPremium = function (order: PublicOrder, premium: number): boolean {
  const orderPremium = parseFloat(order.premium);
  if (order.type === 0) {
    return orderPremium >= premium;
  } else {
    return orderPremium <= premium;
  }
};

const filterOrders = function ({
  federation,
  baseFilter,
  premium = null,
  paymentMethods = [],
  amountFilter = null,
  orders,
}: FilterOrders): PublicOrder[] {
  const enabledCoordinators = federation
    .getCoordinators()
    .filter((coord) => coord.enabled)
    .map((coord) => coord.shortAlias);
  const filteredOrders = (orders ?? Object.values(federation.book)).filter((order) => {
    if (!order) return false;

    const coordinatorCheck = [...enabledCoordinators, ...Object.keys(thirdParties)].includes(
      order.coordinatorShortAlias ?? '',
    );
    const typeChecks = order.type === baseFilter.type || baseFilter.type === null;
    // Roblosats: currency 1000 is Spamcoin, an ordinary payment unit (and the unit of many
    // "丰 sat" orders), not upstream's swap pseudo-currency: never hide it by mode. Picking
    // Spamcoin in the currency filter still narrows the book to it (currencyChecks).
    const premiumChecks = premium !== null ? filterByPremium(order, premium) : true;
    const currencyChecks = order.currency === baseFilter.currency || baseFilter.currency === 0;
    const paymentMethodChecks =
      paymentMethods.length > 0 ? filterByPayment(order, paymentMethods) : true;
    const amountChecks = amountFilter !== null ? filterByAmount(order, amountFilter) : true;
    const hostChecks = filterByHost(order, baseFilter.coordinator, federation);
    return (
      coordinatorCheck &&
      typeChecks &&
      premiumChecks &&
      currencyChecks &&
      paymentMethodChecks &&
      amountChecks &&
      hostChecks
    );
  });
  return filteredOrders.filter((o): o is PublicOrder => o != null);
};

export default filterOrders;
