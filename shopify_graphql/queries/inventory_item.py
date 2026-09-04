class InventoryItemHelper:

    INVENTORY_ITEM_UPDATE_MUTATION = """
    mutation inventoryItemUpdate($id: ID!, $input: InventoryItemInput!) {
      inventoryItemUpdate(id: $id, input: $input) {
        inventoryItem {
          id
          harmonizedSystemCode
        }
        userErrors {
          field
          message
        }
      }
    }
    """

    def __init__(self, client, **kwargs):
        self.client = client
        self.setting = kwargs

    def update_inventory_item(self, inventory_item_id, hsn_code):
        input_data = {
            "harmonizedSystemCode": hsn_code
        }
        variables = {"id": inventory_item_id, "input": input_data}
        return self.client.execute(self.INVENTORY_ITEM_UPDATE_MUTATION, variables)
