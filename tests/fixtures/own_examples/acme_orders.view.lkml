# A synthetic LookML view for the own-examples intake tests (#645).
view: orders {
  sql_table_name: orders ;;

  dimension: id {
    primary_key: yes
    type: number
    sql: ${TABLE}.id ;;
  }
  dimension: status {
    type: string
    sql: ${TABLE}.status ;;
  }
  measure: delivered_revenue {
    label: "Delivered revenue"
    type: sum
    sql: ${TABLE}.total_amount ;;
    filters: [status: "delivered"]
  }
  measure: order_count {
    type: count
  }
  measure: revenue_per_order {
    type: number
    sql: ${delivered_revenue} / NULLIF(${order_count}, 0) ;;
  }
  measure: open_orders {
    type: count
    filters: [status: "-cancelled,-delivered"]
  }
}

view: big_orders {
  derived_table: {
    sql: SELECT id, total_amount FROM orders WHERE total_amount > 100 ;;
  }
  measure: big_order_count {
    type: count_distinct
    sql: ${TABLE}.id ;;
  }
}

explore: orders {
  join: customers {
    sql_on: ${orders.customer_id} = ${customers.id} ;;
    relationship: many_to_one
  }
}
