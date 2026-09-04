class DraftOrderQueryHelper:
    def __init__(self, client):
        self.client = client

    def get_draft_order(self, draft_order_id, fields=None):
        fields = fields or "id name email totalPrice"
        query = f'''
        {{
          draftOrder(id: "gid://shopify/DraftOrder/{draft_order_id}") {{
            {fields}
          }}
        }}
        '''
        return self.client.execute(query)

    def list_draft_orders(self, first=10, fields=None):
        fields = fields or "id name email totalPrice"
        query = f'''
        {{
          draftOrders(first: {first}) {{
            edges {{
              node {{
                {fields}
              }}
            }}
          }}
        }}
        '''
        return self.client.execute(query)

    def create_draft_order(self, draft_order_input, fields=None):
        fields = fields or "draftOrder { id name }"
        mutation = f'''
        mutation {{
          draftOrderCreate(input: {draft_order_input}) {{
            {fields}
            userErrors {{ field message }}
          }}
        }}
        '''
        return self.client.execute(mutation)

    def complete_draft_order(self, draft_order_id, fields=None):
        fields = fields or "draftOrder { id completedAt }"
        mutation = f'''
        mutation {{
          draftOrderComplete(id: "gid://shopify/DraftOrder/{draft_order_id}") {{
            {fields}
            userErrors {{ field message }}
          }}
        }}
        '''
        return self.client.execute(mutation)

    def delete_draft_order(self, draft_order_id, fields=None):
        fields = fields or "deletedId"
        mutation = f'''
        mutation {{
          draftOrderDelete(id: "gid://shopify/DraftOrder/{draft_order_id}") {{
            {fields}
            userErrors {{ field message }}
          }}
        }}
        '''
        return self.client.execute(mutation)
