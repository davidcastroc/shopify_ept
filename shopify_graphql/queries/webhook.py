class WebhookQueryHelper:
    @staticmethod
    def build_delete_webhook_mutation(webhook_id):
        """
        Returns the GraphQL mutation for deleting a webhook subscription.
        :param webhook_id: The ID of the webhook subscription to delete (must be a GQL ID string)
        """
        return f'''
        mutation webhookSubscriptionDelete {{
          webhookSubscriptionDelete(id: "{webhook_id}") {{
            userErrors {{
              field
              message
            }}
            deletedWebhookSubscriptionId
          }}
        }}
        '''

    def __init__(self, client, **kwargs):
        """
        Helper for Shopify Webhook GraphQL queries.
        :param client: GraphQL client (should handle access token, endpoint)
        :param kwargs: Additional settings (e.g., payout_visible_currency)
        """
        self.client = client
        self.settings = kwargs

    @staticmethod
    def build_create_webhook_mutation(topic, callback_url, format="JSON"):
        return f'''
        mutation webhookCreate {{
          webhookSubscriptionCreate(
            topic: {topic}
            webhookSubscription: {{callbackUrl: "{callback_url}", format: {format}}}
          ) {{
            webhookSubscription {{
              id
              topic
              filter
              callbackUrl
            }}
            userErrors {{
              field
              message
            }}
          }}
        }}
        '''

    @staticmethod
    def build_get_webhook_subscriptions_query(first=10):
        """
        Returns the GraphQL query for fetching webhook subscriptions.
        :param first: Number of webhook subscriptions to fetch (default 10)
        """
        return f'''
            query {{
              webhookSubscriptions(first: {first}) {{
                edges {{
                  node {{
                    id
                    topic
                    endpoint {{
                      __typename
                      ... on WebhookHttpEndpoint {{
                        callbackUrl
                      }}
                      ... on WebhookEventBridgeEndpoint {{
                        arn
                      }}
                      ... on WebhookPubSubEndpoint {{
                        pubSubProject
                        pubSubTopic
                      }}
                    }}
                  }}
                }}
              }}
            }}
                '''
