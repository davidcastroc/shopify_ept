def format_graphql_query(query):
    return " ".join(line.strip() for line in query.splitlines())

def parse_graphql_response(response):
    if 'errors' in response:
        raise Exception(response['errors'])
    return response.get('data', {})
