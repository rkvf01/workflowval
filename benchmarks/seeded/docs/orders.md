# Orders API guide

The Orders API is used by shop staff and by the storefront on behalf of customers. Staff ship and
refund orders. Customers place, pay for and cancel them.

## Order lifecycle

1. A customer creates an order. It starts as `pending`.
2. The customer pays the exact total, and the order becomes `paid`.
3. Staff ship a paid order, and it becomes `shipped`.

## Business rules

- **Payment:** a payment must equal the order's total. Only a pending order can be paid.
- **Shipping:** only a paid order can be shipped.
- **Cancelling:** a pending or paid order can be cancelled, fully or in part. Once an order has
  shipped it can no longer be cancelled.
- **Refunds:** a refund pays out the value of the items cancelled from a paid order. After a partial
  cancel, the customer gets back only the value of the cancelled items, and the rest of the order
  stands. A full cancel of a paid order followed by a refund makes the order `refunded`.
- **Safe retries:** send an `Idempotency-Key` header when creating an order. If the same key is sent
  again, you get the first order back and no second order is created, so a retry after a timeout
  never charges a customer twice.

## Listing orders

Results are paged, and each order appears on exactly one page. A page can hold at most 50 orders. Use
`customer` and `status` to filter. A filter that matches nothing returns an empty page.

## Security

Every request needs a bearer token, including every action that changes an order.
