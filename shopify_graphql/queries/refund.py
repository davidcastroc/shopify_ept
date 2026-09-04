class RefundQueryHelper:
    def __init__(self, client):
        self.client = client

    def get_refund(self, refund_id, fields=None):
        fields = fields or "id status processedAt"
        query = f'''
        {{
          refund(id: "gid://shopify/Refund/{refund_id}") {{
            {fields}
          }}
        }}
        '''
        return self.client.execute(query)

    def create_refund(self, refund_input, fields=None):
        fields = fields or "refund { id status }"
        mutation = f'''
        mutation {{
          refundCreate(input: {refund_input}) {{
            {fields}
            userErrors {{ field message }}
          }}
        }}
        '''
        return self.client.execute(mutation)
