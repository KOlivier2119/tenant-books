"""Background jobs (RQ) for invoice artefacts and delivery.

`frappe.enqueue` puts a call on a Redis queue; a worker process picks it up.
Queues used here: "long" for anything that touches the filesystem or the
network (PDF, webhook), so a slow receiver can never block short jobs.
"""

import frappe
from frappe.utils.background_jobs import enqueue


def enqueue_invoice_jobs(invoice_name: str) -> None:
    """Called from Invoice.on_submit.

    enqueue_after_commit=True is important: without it the worker could start
    before the Invoice row is committed and fail with "Invoice not found".
    deduplicate=True plus a job_id stops a double-click from queueing two PDFs.
    """
    enqueue(
        "tenantbooks.billing.jobs.generate_invoice_pdf",
        queue="long",
        job_id=f"tenantbooks-pdf-{invoice_name}",
        deduplicate=True,
        enqueue_after_commit=True,
        invoice_name=invoice_name,
    )
    enqueue(
        "tenantbooks.billing.webhooks.deliver_webhook",
        queue="long",
        job_id=f"tenantbooks-webhook-{invoice_name}",
        deduplicate=True,
        enqueue_after_commit=True,
        invoice_name=invoice_name,
    )


def generate_invoice_pdf(invoice_name: str) -> str:
    """Render the standard print format to PDF and attach it to the Invoice."""
    invoice = frappe.get_doc("Invoice", invoice_name)

    # get_print(doctype, name, as_pdf=True) returns PDF bytes; it shells out to
    # wkhtmltopdf, which is why this job belongs on the "long" queue.
    pdf = frappe.get_print("Invoice", invoice_name, as_pdf=True)

    file_doc = frappe.get_doc(
        {
            "doctype": "File",
            "attached_to_doctype": "Invoice",
            "attached_to_name": invoice.name,
            "content": pdf,
            "file_name": f"{invoice_name}.pdf",
            "is_private": 0,
        }
    )
    file_doc.save(ignore_permissions=True)
    frappe.db.commit()

    invoice.db_set("invoice_pdf", file_doc.file_url, update_modified=False)
    return file_doc.file_url
