import logging

_logger = logging.getLogger("Graphql Channel")


class ChannelQueryHelper:
    
    def __init__(self, client, **kwargs):
        self.client = client
        self.settings = kwargs
    
    CHANNEL_FIELDS = """
                        id
                        name
                        app {
                            id
                        }
                    """
    
    def fetch_all_channels(self):
        """
        Fetch all Shopify sales channels using pagination via the `channels` query.
        :return: list of channel dicts
        """
        channels = []
        has_next_page = True
        cursor = None
        
        while has_next_page:
            after_clause = f', after: "{cursor}"' if cursor else ""
            query = f"""
            {{
              channels(first: 50{after_clause}) {{
                nodes {{
                  {self.CHANNEL_FIELDS}
                }}
                pageInfo {{
                  hasNextPage
                  endCursor
                }}
              }}
            }}
            """
            try:
                response = self.client.execute(query)
            except Exception as e:
                _logger.error("Error fetching Shopify channels: %s", e)
                raise
            
            channel_data = response.get("data", {}).get("channels", {})
            nodes = channel_data.get("nodes", [])
            channels.extend(nodes)
            
            page_info = channel_data.get("pageInfo", {})
            has_next_page = page_info.get("hasNextPage", False)
            cursor = page_info.get("endCursor")
        
        return channels
    
    def get_channel_by_id(self, gid):
        """
        Fetch a single channel by its GID.
        :param gid: str – e.g. "gid://shopify/Channel/12345"
        :return: dict or None
        """
        query = f"""
        {{
          channel(id: "{gid}") {{
            {self.CHANNEL_FIELDS}
          }}
        }}
        """
        try:
            response = self.client.execute(query)
            return response.get("data", {}).get("channel")
        except Exception as e:
            _logger.error("Error fetching Shopify channel %s: %s", gid, e)
            return None
    
    @staticmethod
    def gid_to_int(gid):
        """Convert Shopify GID → integer ID."""
        if not gid:
            return None
        try:
            return int(gid.split("/")[-1])
        except Exception:
            return gid
