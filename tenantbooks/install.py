"""One-time setup that runs when the app is installed on a site."""

import frappe

# Roles this app expects.  DocType JSON files reference these names in their
# permission rows, so the roles must exist before the permissions make sense.
ROLES = ["Billing Manager", "Billing Clerk"]


def after_install():
    for role_name in ROLES:
        if not frappe.db.exists("Role", role_name):
            # `ignore_permissions=True` is needed because the installing user is
            # the only one allowed to create roles, and the Role DocType is
            # restricted to System Manager.
            frappe.get_doc({"doctype": "Role", "role_name": role_name}).insert(ignore_permissions=True)
    frappe.db.commit()
