class BulkOrderQueryHelper:
    def __init__(self, client):
        self.client = client

    def run_bulk_orders_query(self, custom_query):
        return self.client.bulk_operation(custom_query)
