# -*- coding: utf-8 -*-
# See LICENSE file for full copyright and licensing details.

import hashlib
import logging
from collections import defaultdict

_logger = logging.getLogger("Shopify Translation")


def get_digest(value: str) -> str:
    """SHA-256 digest of a string — required by Shopify translationsRegister."""
    return hashlib.sha256(value.encode()).hexdigest()


def format_gid_alias(gid: str, locale: str) -> str:
    """
    Convert 'gid://shopify/Product/123' + 'fr' → 'Product_123_fr'
    This creates a valid GraphQL alias (no slashes or colons).
    """
    parts = gid.split("/")
    return f"{parts[-2]}_{parts[-1]}_{locale}"


class TranslationQueryHelper:
    """
    Handles all Shopify Translations API calls for multi-language product sync.

    IMPORT (pull):
      - add_target_to_pull(gid, locale) — register locales to pull
      - pull_translations()             — ONE batched query for all locales

    EXPORT (push):
      - fetch_primary_content(gid)      — get primary lang content + digest from Shopify
      - add_target_to_push_with_digest  — register a translation with Shopify's own digest
      - push_translations()             — ONE batched mutation for all locales + fields
    """

    # ------------------------------------------------------------------ #
    # GraphQL field fragments
    # ------------------------------------------------------------------ #

    TRANSLATION_FIELDS = "key value locale outdated"
    TRANSLATABLE_CONTENT_FIELDS = "key value locale digest"
    USER_ERRORS_FIELDS = "field message"

    def __init__(self, client):
        self.client = client
        self._pull_targets = []   # list of (gid, locale)
        self._push_targets = []   # list of {gid, key, value, digest, locale}

    # ------------------------------------------------------------------ #
    # IMPORT — Pull translations from Shopify
    # ------------------------------------------------------------------ #

    def add_target_to_pull(self, gid: str, locale: str):
        """Register a (gid, locale) pair for the next batched pull query."""
        self._pull_targets.append((gid, locale))

    def pull_translations(self) -> list:
        """
        Execute ONE batched GraphQL query for all registered (gid, locale) pairs.

        Returns list of dicts:
          [
            {
              'gid':          'gid://shopify/Product/123',
              'locale':       'fr',
              'translations': {'title': 'Produit', 'body_html': '...'},
              'primary':      {'title': 'My Product', 'body_html': '...'},
            },
            ...
          ]
        """
        if not self._pull_targets:
            return []

        # Build one big batched query
        fragments = []
        for gid, locale in self._pull_targets:
            alias = format_gid_alias(gid, locale)
            fragments.append(f"""
                {alias}: translatableResource(resourceId: "{gid}") {{
                    resourceId
                    translations(locale: "{locale}") {{
                        {self.TRANSLATION_FIELDS}
                    }}
                    translatableContent {{
                        {self.TRANSLATABLE_CONTENT_FIELDS}
                    }}
                }}
            """)

        query = "query { %s }" % "\n".join(fragments)
        response = self.client.execute(query)
        data = response.get("data", {}) or {}

        results = []
        for gid, locale in self._pull_targets:
            alias = format_gid_alias(gid, locale)
            node = data.get(alias)
            if not node:
                _logger.warning(
                    "pull_translations: no data for alias '%s' (gid=%s, locale=%s)",
                    alias, gid, locale,
                )
                continue

            translations = {
                t["key"]: t["value"]
                for t in (node.get("translations") or [])
                if t.get("value")
            }
            # primary content: used to detect "unchanged" translations (Shopify sometimes
            # returns the primary-language value as a translation for locales that have
            # no explicit override — we need to filter those out).
            primary = {
                c["key"]: c["value"]
                for c in (node.get("translatableContent") or [])
                if c.get("value")
            }

            results.append({
                "gid":          node.get("resourceId", gid),
                "locale":       locale,
                "translations": translations,
                "primary":      primary,
            })

        self._pull_targets.clear()
        return results

    # ------------------------------------------------------------------ #
    # EXPORT — Fetch primary content + push translations to Shopify
    # ------------------------------------------------------------------ #

    def fetch_primary_content(self, gid: str) -> dict:
        """
        Fetch the primary language's translatableContent for a product GID.

        Returns:
          {
            'title':     {'value': 'My Product',  'digest': 'sha256abc...'},
            'body_html': {'value': '<p>Desc</p>', 'digest': 'sha256xyz...'},
          }

        The digest is Shopify's own SHA-256 of the primary value —
        it MUST be passed when calling translationsRegister.
        """
        query = """
            query {
                translatableResource(resourceId: "%s") {
                    resourceId
                    translatableContent {
                        %s
                    }
                }
            }
        """ % (gid, self.TRANSLATABLE_CONTENT_FIELDS)

        response = self.client.execute(query)
        content_list = (
            response.get("data", {})
                    .get("translatableResource", {})
                    .get("translatableContent", [])
        ) or []

        return {
            item["key"]: {
                "value":  item.get("value", ""),
                "digest": item.get("digest", ""),
            }
            for item in content_list
            if item.get("key")
        }

    def add_target_to_push_with_digest(
        self, gid: str, key: str, value: str, digest: str, locale: str
    ):
        """
        Register one translation for the batched push using Shopify's own digest.

        :param gid:    Shopify GID  e.g. 'gid://shopify/Product/123'
        :param key:    Field key    e.g. 'title' or 'body_html'
        :param value:  Translated value
        :param digest: SHA-256 digest returned by fetch_primary_content() — never recompute
        :param locale: Shopify locale code  e.g. 'fr'
        """
        if not value or not digest:
            return
        self._push_targets.append({
            "gid":    gid,
            "key":    key,
            "value":  value,
            "digest": digest,
            "locale": locale,
        })

    def push_translations(self) -> list:
        """
        Execute ONE batched translationsRegister mutation for all registered targets.
        Groups by (gid, locale) into separate mutation aliases.

        Returns list of registered translation dicts from Shopify.
        """
        if not self._push_targets:
            return []

        # Group by (gid, locale) alias
        grouped = defaultdict(list)
        alias_to_gid = {}

        for item in self._push_targets:
            alias = format_gid_alias(item["gid"], item["locale"])
            grouped[alias].append({
                "key":                       item["key"],
                "value":                     item["value"],
                "locale":                    item["locale"],
                "translatableContentDigest": item["digest"],
            })
            alias_to_gid[alias] = item["gid"]

        # Build mutation arguments + body
        args_parts = []
        body_parts = []
        variables = {}

        for alias, translations in grouped.items():
            gid_var   = alias
            trans_var = f"{alias}_translations"

            args_parts.append(f"${gid_var}: ID!, ${trans_var}: [TranslationInput!]!")
            body_parts.append(f"""
                {alias}: translationsRegister(
                    resourceId: ${gid_var},
                    translations: ${trans_var}
                ) {{
                    translations {{ {self.TRANSLATION_FIELDS} }}
                    userErrors    {{ {self.USER_ERRORS_FIELDS} }}
                }}
            """)
            variables[gid_var]   = alias_to_gid[alias]
            variables[trans_var] = translations

        mutation = "mutation translationsRegisterMultiple(%s) { %s }" % (
            ", ".join(args_parts),
            "\n".join(body_parts),
        )

        response = self.client.execute(mutation, variables)
        data = response.get("data", {}) or {}

        # Collect and log errors (non-blocking)
        for alias_data in data.values():
            if alias_data and alias_data.get("userErrors"):
                _logger.warning(
                    "translationsRegister userErrors: %s", alias_data["userErrors"]
                )

        # Collect results
        result = []
        for alias_data in data.values():
            if alias_data and alias_data.get("translations"):
                result.extend(alias_data["translations"])

        self._push_targets.clear()
        return result


    def fetch_shop_locales(self) -> list:
        """
        Fetch all published locales from the Shopify store.
        """
        query = """
            query {
                shopLocales(published: true) {
                    locale
                    name
                    primary
                    published
                }
            }
        """
        response = self.client.execute(query)

        errors = response.get("errors")
        if errors:
            raise RuntimeError(f"Shopify GraphQL errors while fetching shop locales: {errors}")

        locales = (response.get("data") or {}).get("shopLocales", [])
        _logger.info("Fetched %d published shop locale(s) from Shopify.", len(locales))
        return locales

    def clear(self):
        """Reset all pending targets."""
        self._pull_targets.clear()
        self._push_targets.clear()

