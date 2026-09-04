# -*- coding: utf-8 -*-
# See LICENSE file for full copyright and licensing details.
import logging
from datetime import datetime, timedelta
from odoo import models, fields, api, _
from odoo.exceptions import UserError
from .. import shopify_graphql

_logger = logging.getLogger("Shopify Customer Segment")


class ShopifyCustomerSegmentEpt(models.Model):
    _name = "shopify.customer.segment.ept"
    _description = "Shopify Customer Segment"
    _rec_name = "name"

    name = fields.Char(string="Segment Name")
    shopify_segment_id = fields.Char(string="Shopify Segment GID", readonly=True)
    shopify_instance_id = fields.Many2one("shopify.instance.ept", string="Instance", required=True)
    query = fields.Text(string="Segment Query", readonly=True)
    last_synced_on = fields.Datetime(string="Last Synced On", readonly=True)
    odoo_tag_id = fields.Many2one(comodel_name="res.partner.category", string="Odoo Tag",
                                  help="Odoo partner tag that represents this Shopify segment.")
    member_ids = fields.One2many(comodel_name="shopify.customer.segment.member.ept", inverse_name="segment_id",
                                 string="Members", readonly=True)
    partner_count = fields.Integer(string="Partners Tagged", compute="_compute_partner_count")

    @api.depends("shopify_segment_id", "shopify_instance_id")
    def _compute_partner_count(self):
        for record in self:
            record.partner_count = self.env["res.partner"].search_count(
                [("shopify_segment_ids", "in", record.ids)]
            )

    @api.model
    def import_customer_segment(self, instance):
        """
        Fetch all customer segments from Shopify and create/update the
        corresponding records in Odoo.
        :param instance: shopify.instance.ept record
        """
        last_sync = instance.last_customer_segment_import_date
        if last_sync:
            # Fetch segments edited in the last 7 days relative to last sync date
            from_date = last_sync - timedelta(days=7)
            last_edit_date_from = from_date.strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            last_edit_date_from = None

        try:
            client = instance.get_graphql_client()
            helper = shopify_graphql.SegmentQueryHelper(client)
            segments = helper.get_shopify_segments(last_edit_date_from=last_edit_date_from)
        except Exception as exc:
            _logger.error("Error fetching segments from Shopify: %s", exc)
            raise UserError(_("Error fetching customer segments:\n%s") % str(exc))

        if not segments:
            _logger.info("No customer segments found in Shopify for instance '%s'.", instance.name)
            return True

        for seg in segments:
            gid = seg.get("id")
            name = seg.get("name", "").strip()
            query_text = seg.get("query", "")

            existing = self.search(
                [("shopify_segment_id", "=", gid), ("shopify_instance_id", "=", instance.id)],
                limit=1,
            )
            vals = {
                "name": name,
                "shopify_segment_id": gid,
                "shopify_instance_id": instance.id,
                "query": query_text,
                "last_synced_on": datetime.now(),
            }
            if existing:
                existing.write(vals)
            else:
                self.create(vals)

        # ------------------------------------------------------------------
        # Detect and remove segments deleted from Shopify.
        # A separate full (no date filter) fetch gives us all currently active
        # GIDs; any Odoo record whose GID is absent has been deleted in Shopify.
        # ------------------------------------------------------------------
        try:
            all_active_segments = helper.get_shopify_segments()
            all_shopify_gids = {s.get("id") for s in all_active_segments if s.get("id")}
            stale_segments = self.search([
                ("shopify_instance_id", "=", instance.id),
                ("shopify_segment_id", "not in", list(all_shopify_gids)),
            ])
            if stale_segments:
                _logger.info(
                    "Instance '%s': removing %d segment(s) deleted from Shopify: %s",
                    instance.name,
                    len(stale_segments),
                    stale_segments.mapped("name"),
                )
                stale_segments._cleanup_before_unlink()
                stale_segments.unlink()
        except Exception as exc:
            _logger.warning(
                "Could not check for deleted segments for instance '%s': %s",
                instance.name, exc,
            )

        instance.write({"last_customer_segment_import_date": datetime.now()})
        return True

    def sync_segment_members(self):
        """
        For each selected segment record:
        1. Fetch customerSegmentMembers from Shopify via GQL.
        2. Ensure a matching Odoo tag (res.partner.category) exists.
        3. Match each Shopify member to a res.partner by email.
        4. Add the tag + link the segment on matched partners.
        5. Remove the tag + unlink the segment from partners that are no
           longer members of this segment in Shopify.
        """
        for segment in self:
            if not segment.shopify_segment_id:
                _logger.warning("Segment '%s' has no Shopify GID – skipping.", segment.name)
                continue

            instance = segment.shopify_instance_id
            try:
                client = instance.get_graphql_client()
                helper = shopify_graphql.SegmentQueryHelper(client)
                members = helper.get_segment_members(segment.shopify_segment_id)
            except Exception as exc:
                _logger.error("Error fetching members for segment '%s': %s", segment.name, exc)
                raise UserError(_("Error fetching members for segment '%s':\n%s") % (segment.name, str(exc)))

            # Ensure the Odoo tag exists
            tag = segment._get_or_create_odoo_tag()

            # Collect emails of Shopify members
            shopify_emails = set()
            for member in members:
                email_obj = member.get("defaultEmailAddress") or {}
                email = (email_obj.get("emailAddress") or "").strip().lower()
                if email:
                    shopify_emails.add(email)

            partner_obj = self.env["res.partner"]
            member_obj = self.env["shopify.customer.segment.member.ept"]

            # Build email → partner map for quick lookup
            partners_by_email = {}
            if shopify_emails:
                for partner in partner_obj.search([("email", "in", list(shopify_emails))]):
                    partners_by_email[(partner.email or "").strip().lower()] = partner

            # Wipe existing member records for this segment and recreate
            segment.member_ids.unlink()

            member_vals_list = []
            for member in members:
                email_obj = member.get("defaultEmailAddress") or {}
                email = (email_obj.get("emailAddress") or "").strip().lower()
                amount_info = member.get("amountSpent") or {}
                matched_partner = partners_by_email.get(email)
                member_vals_list.append({
                    "segment_id": segment.id,
                    "shopify_member_id": member.get("id"),
                    "shopify_display_name": member.get("displayName"),
                    "first_name": member.get("firstName"),
                    "last_name": member.get("lastName"),
                    "email": email or False,
                    "last_order_id": member.get("lastOrderId"),
                    "number_of_orders": member.get("numberOfOrders") or 0,
                    "amount_spent": float(amount_info.get("amount") or 0),
                    "currency_code": amount_info.get("currencyCode"),
                    "partner_id": matched_partner.id if matched_partner else False,
                })
            if member_vals_list:
                member_obj.sudo().create(member_vals_list)

            # ---- tag / segment linking on res.partner ----
            new_partners = self.env["res.partner"].browse([p.id for p in partners_by_email.values()])

            # Partners currently linked to this segment
            existing_partners = partner_obj.search([("shopify_segment_ids", "in", segment.ids)])

            # Re-apply missing tag/link as well (e.g., tag was manually removed).
            partners_to_add = new_partners.filtered(
                lambda p: segment not in p.shopify_segment_ids or tag not in p.category_id
            )
            if partners_to_add:
                partners_to_add.write({"category_id": [(4, tag.id)], "shopify_segment_ids": [(4, segment.id)]})
                _logger.info("Segment '%s': ensured tag/link on %d partner(s).", segment.name, len(partners_to_add))

            # Remove tag + segment link from partners no longer in Shopify segment
            partners_to_remove = existing_partners.filtered(
                lambda p: (p.email or "").strip().lower() not in shopify_emails
            )
            if partners_to_remove:
                partners_to_remove.write({
                    "category_id": [(3, tag.id)],
                    "shopify_segment_ids": [(3, segment.id)],
                })
                _logger.info("Segment '%s': removed tag from %d partner(s) no longer in segment.", segment.name,
                             len(partners_to_remove))

            segment.write({"last_synced_on": datetime.now()})
            _logger.info("Segment '%s' synced: %d Shopify members, %d Odoo partners matched.", segment.name,
                         len(members), len(new_partners))

        return True

    def _get_or_create_odoo_tag(self):
        """
        Return the res.partner.category tag linked to this segment, creating
        it (and storing the link) if it does not yet exist.
        """
        self.ensure_one()
        if self.odoo_tag_id:
            return self.odoo_tag_id
        tag = self.env["res.partner.category"].search([("name", "=ilike", self.name)], limit=1)
        if not tag:
            tag = self.env["res.partner.category"].sudo().create({"name": self.name})
        self.odoo_tag_id = tag
        return tag

    def _cleanup_before_unlink(self):
        """
        Before unlinking a segment record that was deleted in Shopify,
        remove its Odoo tag and the shopify_segment_ids reference from
        every res.partner that was linked to it.
        member_ids are cleaned up automatically via ondelete='cascade'.
        """
        partner_obj = self.env["res.partner"]
        for segment in self:
            linked_partners = partner_obj.search([("shopify_segment_ids", "in", segment.ids)])
            if linked_partners:
                write_vals = {"shopify_segment_ids": [(3, segment.id)]}
                if segment.odoo_tag_id:
                    write_vals["category_id"] = [(3, segment.odoo_tag_id.id)]
                linked_partners.write(write_vals)
                _logger.info("Segment '%s' (deleted in Shopify): removed tag/link from %d partner(s).", segment.name,
                             len(linked_partners))

