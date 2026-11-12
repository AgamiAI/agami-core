-- Orders placed, not counting cancelled ones
SELECT COUNT(*) AS orders FROM orders WHERE status != 'cancelled';

/* Revenue from delivered orders; a ';' inside a string must not split it */
SELECT SUM(total_amount) AS revenue FROM orders WHERE status = 'delivered' AND channel <> 'x;y';

DELETE FROM orders;
