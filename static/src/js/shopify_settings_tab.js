/** @odoo-module **/

import { patch }      from "@web/core/utils/patch";
import { RadioField } from "@web/views/fields/radio/radio_field";
import { onMounted }  from "@odoo/owl";

const SHOPIFY_TAB_KEY = "shopify_config_tab_v19";

patch(RadioField.prototype, {
    setup() {
        super.setup(...arguments);
        if (this.props.name !== "shopify_config_tab") return;
        onMounted(() => {
            const saved = localStorage.getItem(SHOPIFY_TAB_KEY);
            if (!saved) return;
            const current = this.props.record.data[this.props.name];
            if (saved === current) return;
            const selection = this.props.record.fields[this.props.name]?.selection ?? [];
            const isValid = selection.some(([key]) => key === saved);
            if (isValid) {
                this.props.record.update({ [this.props.name]: saved });
            }
        });
    },

    onChange(value) {
        if (this.props.name === "shopify_config_tab") {
            try {
                localStorage.setItem(SHOPIFY_TAB_KEY, value);
            } catch (_) {}
        }
        return super.onChange(value);
    },
});