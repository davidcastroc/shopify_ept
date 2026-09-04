import time
import json
import logging
import pytz
from odoo import models, fields

from ..shopify.pyactiveresource.connection import ClientError
from .. import shopify
from ..shopify_graphql.client import ShopifyGraphQLClient
from ..shopify_graphql.queries.inventory import InventoryQueryHelper

utc = pytz.utc

_logger = logging.getLogger("Shopify Export Stock Queue Line")


class ShopifyExportStockQueueLineEpt(models.Model):
    _name = "shopify.export.stock.queue.line.ept"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _description = "Shopify Export Stock Queue Line"

    name = fields.Char()
    shopify_instance_id = fields.Many2one("shopify.instance.ept", string="Instance")
    last_process_date = fields.Datetime()
    inventory_item_id = fields.Char()
    location_id = fields.Char()
    quantity = fields.Integer()
    shopify_product_id = fields.Many2one('shopify.product.product.ept', string="Product")
    state = fields.Selection([("draft", "Draft"), ("failed", "Failed"), ("done", "Done"),
                              ("cancel", "Cancelled")],
                             default="draft")
    export_stock_queue_id = fields.Many2one("shopify.export.stock.queue.ept", required=True,
                                            ondelete="cascade", copy=False)
    common_log_lines_ids = fields.One2many("common.log.lines.ept",
                                           "shopify_export_stock_queue_line_id",
                                           help="Log lines created against which line.")


    def auto_export_stock_queue_data(self):
        """
        This method is used to find export stock queue which queue lines have state
        in draft and is_action_require is False.
        @author: Nilam Kubavat @Emipro Technologies Pvt.Ltd on date 31-Aug-2022.
        Task Id : 199065
        """
        export_stock_queue_obj = self.env["shopify.export.stock.queue.ept"]
        export_stock_queue_ids = []
        query = """
            UPDATE shopify_export_stock_queue_ept
            SET is_process_queue = %s
            WHERE is_process_queue = %s
        """
        params = (False, True)

        self.env.cr.execute(query, params)
        self.env.cr.commit()
        query = """
            SELECT DISTINCT queue.id
            FROM shopify_export_stock_queue_line_ept AS queue_line
            INNER JOIN shopify_export_stock_queue_ept AS queue
            ON queue_line.export_stock_queue_id = queue.id
            WHERE queue_line.state IN (%s) AND queue.is_action_require = %s
            GROUP BY queue.id
            ORDER BY queue.id
        """
        params = ('draft', False)

        self.env.cr.execute(query, params)

        export_stock_queue_list = self.env.cr.fetchall()
        if not export_stock_queue_list:
            return True

        export_stock_queue_ids = [result[0] for result in export_stock_queue_list]
        # for result in export_stock_queue_list:
        #     if result[0] not in export_stock_queue_ids:
        #         export_stock_queue_ids.append(result[0])

        queues = export_stock_queue_obj.browse(export_stock_queue_ids)
        self.filter_export_stock_queue_lines_and_post_message(queues)

    def filter_export_stock_queue_lines_and_post_message(self, queues):
        """
        This method is used to post a message if the queue is process more than 3 times otherwise
        it calls the child method to process the export stock queue line.
        @author: Nilam Kubavat @Emipro Technologies Pvt.Ltd on date 31-Aug-2022.
        Task Id : 199065
        """
        common_log_line_obj = self.env["common.log.lines.ept"]
        start = time.time()
        export_stock_queue_process_cron_time = queues.shopify_instance_id.get_shopify_cron_execution_time(
            "shopify_ept.process_shopify_export_stock_queue")

        for queue in queues:
            export_stock_queue_line_ids = queue.export_stock_queue_line_ids.filtered(lambda x: x.state == "draft")

            # For counting the queue crashes and creating schedule activity for the queue.
            queue.queue_process_count += 1
            if queue.queue_process_count > 3:
                queue.is_action_require = True
                note = "<p>Need to process this export stock queue manually.There are 3 attempts been made by " \
                       "automated action to process this queue,<br/>- Ignore, if this queue is already processed.</p>"
                queue.message_post(body=note)
                if queue.shopify_instance_id.is_shopify_create_schedule:
                    common_log_line_obj.create_crash_queue_schedule_activity(queue, "shopify.export.stock.queue.ept",
                                                                             note)
                continue

            self.env.cr.commit()
            export_stock_queue_line_ids.process_export_stock_queue_data()
            if time.time() - start > export_stock_queue_process_cron_time - 60:
                return True

    def process_export_stock_queue_data(self):
        """
        This method is used to processes export stock queue lines.
        @author: Nilam Kubavat @Emipro Technologies Pvt.Ltd on date 31-Aug-2022.
        Task Id : 199065
        """
        common_log_line_obj = self.env['common.log.lines.ept']
        model = "shopify.export.stock.queue.ept"
        queue_id = self.export_stock_queue_id if len(self.export_stock_queue_id) == 1 else False
        if queue_id:
            instance = queue_id.shopify_instance_id
            instance.connect_in_shopify()
            query = """
                UPDATE shopify_export_stock_queue_ept
                SET is_process_queue = %s
                WHERE is_process_queue = %s
            """
            params = (False, True)
            self.env.cr.execute(query, params)
            self.env.cr.commit()
            if instance.use_graphql_api:
                self._prepare_data_and_export_stock_by_graphql(self)
            else:
                for queue_line in self:
                    log_line = False
                    shopify_product = queue_line.shopify_product_id
                    odoo_product = shopify_product.product_id
                    try:
                        shopify.InventoryLevel.set(queue_line.location_id, queue_line.inventory_item_id,
                                                   queue_line.quantity)
                    except ClientError as error:
                        if hasattr(error,
                                   "response") and error.response.code == 429 and error.response.msg == "Too Many Requests":
                            time.sleep(int(float(error.response.headers.get('Retry-After', 5))))
                            shopify.InventoryLevel.set(queue_line.location_id,
                                                       queue_line.inventory_item_id,
                                                       queue_line.quantity)
                            queue_line.write({"state": "done"})
                            continue
                        if hasattr(error, "response") and error.response.code == 422 and error.response.msg == "Unprocessable Entity":
                            if json.loads(error.response.body.decode()).get("errors")[
                                0] == 'Inventory item does not have inventory tracking enabled':
                                queue_line.shopify_product_id.write({'inventory_management': "Dont track Inventory"})
                                queue_line.write({'state': 'done'})
                            continue
                        if hasattr(error, "response"):
                            message = ("System tried to export stock but received an error from the Shopify store with Product ID: %s and name: %s for the %s instance.\n"
                                          "Action Items:\n"
                                          "- Verify the product's existence on the Shopify store using the given name and Product ID.\n"
                                          "- If it has been deleted, archive the product from the Shopify product layer "
                                          "in Odoo.") % (odoo_product.id, odoo_product.name, instance.name)
                            log_line = common_log_line_obj.create_common_log_line_ept(shopify_instance_id=instance.id,module="shopify_ept",
                                                                                      message=message,
                                                                                      model_name=model,
                                                                                      shopify_export_stock_queue_line_id=queue_line.id if queue_line else False)
                            queue_line.write({"state": "failed"})
                            continue
                    except Exception as error:
                        message = ("System tried to export stock but received an error from the Shopify store with Product ID: %s and name: %s for the %s instance.\n"
                                      "Action Items:\n"
                                      "- Verify the product's existence on the Shopify store using the given name and Product ID.\n"
                                      "- If it has been deleted, archive the product from the Shopify product layer "
                                      "in Odoo.") % (odoo_product.id, odoo_product.name, instance.name)
                        log_line = common_log_line_obj.create_common_log_line_ept(shopify_instance_id=instance.id,module="shopify_ept",
                                                                                  message=message,
                                                                                  model_name=model,
                                                                                  shopify_export_stock_queue_line_id=queue_line.id if queue_line else False)

                    if not log_line:
                        queue_id.is_process_queue = True
                        queue_line.write({"state": "done"})
                    else:
                        queue_line.write({"state": "failed"})
            self.env.cr.commit()
        return True

    def _prepare_data_and_export_stock_by_graphql(self, queue_lines):
        """
        This Method prepare the data required for the stock export to shopify and export in shopify using the GraphQL API.
        Uses a loop to retry after excluding invalid lines so that ALL bad lines in a batch are
        discovered and logged in a single run — regardless of how many different errors Shopify
        returns across successive attempts.
        @params : queue_lines : all the stock queueline associated with a queue.
        @author : Gopal Chouhan on 14/05/2025
        :param queue_lines:
        :return:
        """
        instance = self.shopify_instance_id
        common_log_line_obj = self.env['common.log.lines.ept']
        model = "shopify.export.stock.queue.ept"
        client = instance.get_graphql_client()
        inventory_helper = InventoryQueryHelper(client)

        # Keep retrying with the remaining (non-failed) lines until the batch succeeds.
        remaining_lines = queue_lines
        max_iterations = len(queue_lines) + 1

        for attempt in range(1, max_iterations + 1):
            if not remaining_lines:
                break
            quantities_payload = inventory_helper.build_quantities_from_queue_lines(remaining_lines)
            stock_result = inventory_helper.set_inventory_quantities(
                quantities_payload, reason='correction', name='available', ignore_compare=True
            )
            if not stock_result:
                # No response at all — nothing we can do this cycle.
                break
            req_error = (
                stock_result.get('data', {})
                .get('inventorySetQuantities', {})
                .get('userErrors')
            )
            req_other_error = stock_result.get('errors')
            if not req_error and req_other_error:
                req_error = req_other_error
            if not req_error:
                # Entire remaining batch was accepted — mark all as done and stop.
                remaining_lines.write({"state": "done"})
                break

            # ── There are errors in this attempt ──────────────────────────────
            remaining_lines_list = list(remaining_lines)
            failed_queue_line_ids = set()
            activated_any = False  # tracks whether any line was auto-activated this round
            resolved_any = False
            for error in (req_error if isinstance(req_error, list) else [req_error]):
                identified_queue_line = None
                message = None  # built below; None means no log entry needed for this error
                # userErrors from inventorySetQuantities carry a field path like:
                # ['input', 'quantities', '<index>', 'inventoryItemId']
                # Use the index to pinpoint the exact queue line within the
                # *current* remaining_lines_list (not the original queue_lines).
                field = error.get('field', []) if isinstance(error, dict) else []
                if isinstance(field, list) and len(field) >= 3 and field[1] == 'quantities':
                    try:
                        line_index = int(field[2])
                        if 0 <= line_index < len(remaining_lines_list):
                            identified_queue_line = remaining_lines_list[line_index]
                    except (ValueError, IndexError):
                        pass

                if identified_queue_line:
                    shopify_product = identified_queue_line.shopify_product_id
                    odoo_product = shopify_product.product_id
                    error_code = error.get('code', 'UNKNOWN') if isinstance(error, dict) else 'UNKNOWN'

                    # Common fields shared by every error message for this queue line.
                    base_message = (
                        "Queue: %s | Queue Line: %s | Attempt: %d of %d\n"
                        "Product           : %s\n"
                        "Inventory Item ID : %s\n"
                        "Location ID       : %s\n"
                        "Quantity          : %s"
                    ) % (
                        identified_queue_line.export_stock_queue_id.name,
                        identified_queue_line.name,
                        attempt, max_iterations,
                        odoo_product.name,
                        identified_queue_line.inventory_item_id,
                        identified_queue_line.location_id,
                        identified_queue_line.quantity,
                    )

                    # ── Auto-activate: inventory item not yet stocked at this location ─
                    if error_code == 'ITEM_NOT_STOCKED_AT_LOCATION':
                        activate_result = inventory_helper.activate_inventory_item(
                            identified_queue_line.inventory_item_id,
                            identified_queue_line.location_id,
                            available=identified_queue_line.quantity,
                        )
                        activate_errors = (
                            (activate_result or {})
                            .get('data', {})
                            .get('inventoryActivate', {})
                            .get('userErrors') or []
                        )
                        if not activate_errors:
                            # Activation succeeded — leave the line in remaining_lines
                            # so the next loop iteration will retry it. No log needed.
                            _logger.info(
                                "Shopify GQL: Auto-activated inventory item %s at location %s "
                                "for queue line %s (Queue: %s). Will retry stock export.",
                                identified_queue_line.inventory_item_id,
                                identified_queue_line.location_id,
                                identified_queue_line.name,
                                identified_queue_line.export_stock_queue_id.name,
                            )
                            activated_any = True
                        else:
                            # Activation itself failed — treat as a real error
                            activate_error_msg = '; '.join(
                                e.get('message', str(e)) for e in activate_errors
                            )
                            message = (
                                "Error while auto-activating inventory location\n"
                                "%s\n"
                                "Original Error    : ITEM_NOT_STOCKED_AT_LOCATION\n"
                                "Activation Error  : %s"
                            ) % (base_message, activate_error_msg)
                            failed_queue_line_ids.add(identified_queue_line.id)
                    elif error_code == 'NON_MUTABLE_INVENTORY_ITEM':
                        message = (
                            "Error while exporting stock\n"
                            "%s\n"
                            "Error Code        : %s\n"
                            "Error Message     : %s\n"
                            "We Marked it Done to avoid further attempts."
                        ) % (
                            base_message,
                            error_code,
                            error.get('message', str(error)) if isinstance(error, dict) else str(error),
                        )
                        identified_queue_line.write({"state": "done"})
                        remaining_lines = remaining_lines - identified_queue_line
                        resolved_any = True
                    else:
                        message = (
                            "Error while exporting stock\n"
                            "%s\n"
                            "Error Code        : %s\n"
                            "Error Message     : %s"
                        ) % (
                            base_message,
                            error_code,
                            error.get('message', str(error)) if isinstance(error, dict) else str(error),
                        )
                        failed_queue_line_ids.add(identified_queue_line.id)
                else:
                    # Fallback: index could not be resolved (e.g. top-level API error).
                    # Link the log to the first remaining line so it is still visible.
                    identified_queue_line = remaining_lines_list[0] if remaining_lines_list else None
                    if identified_queue_line:
                        failed_queue_line_ids.add(identified_queue_line.id)
                    message = (
                        "Error while exporting stock for Queue: %s | Instance: '%s'\n"
                        "Attempt      : %d of %d\n"
                        "Error        : %s\n\n"
                        "Note: The Shopify API response did not include a quantities index, "
                        "so the error has been linked to the first queue line as a fallback."
                    ) % (
                        queue_lines.export_stock_queue_id,
                        instance.name,
                        attempt,
                        max_iterations,
                        str(error),
                    )

                if message:
                    common_log_line_obj.create_common_log_line_ept(
                        shopify_instance_id=instance.id,
                        module="shopify_ept",
                        message=message,
                        model_name=model,
                        shopify_export_stock_queue_line_id=identified_queue_line.id if identified_queue_line else False,
                    )
            # Mark the bad lines found in this attempt as failed and remove them
            # from the batch so the next iteration only sends the remaining valid lines.
            failed_lines = remaining_lines.filtered(lambda l: l.id in failed_queue_line_ids)
            if failed_lines:
                failed_lines.write({"state": "failed"})
            remaining_lines = remaining_lines - failed_lines
            if not failed_queue_line_ids and not activated_any and not resolved_any:
                # Errors were present but none could be mapped to a specific line and
                # no activations were performed — stop to avoid an infinite loop
                # (already logged above).
                if remaining_lines:
                    remaining_lines.write({"state": "failed"})
                break

    # No session to close when using client-based GraphQL

    def _graphql_prepare_stock_data_for_export(self, queuelines):
        """
        Format the export stock data as per the GraphQL APi requirement
        @return string
        @author : Gopal Chouhan on 15 may 2025
        """
        stock_data = []
        for line in queuelines:
            single_data = """{
                inventoryItemId: "gid://shopify/InventoryItem/%s",
                locationId: "gid://shopify/Location/%s",
                quantity: %s}""" % (line.inventory_item_id, line.location_id, line.quantity)
            stock_data.append(single_data)
        final_data = "[%s]" % ", ".join(stock_data)
        return final_data