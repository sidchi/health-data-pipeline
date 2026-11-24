resource "google_cloud_scheduler_job" "daily" {
  project     = var.project_id
  region      = var.region
  name        = "${var.name}-daily"
  description = "Triggers the ${var.name} function."
  schedule    = var.schedule
  time_zone   = var.time_zone

  # A little longer than the function timeout, so Scheduler sees the function's
  # own 500 rather than cutting the request off.
  attempt_deadline = "${google_cloudfunctions2_function.export.service_config[0].timeout_seconds + 20}s"

  # The function returns 500 on any failure; uploads are idempotent (the day's
  # file is replaced in place), so retrying is safe.
  retry_config {
    retry_count          = 3
    min_backoff_duration = "60s"
    max_backoff_duration = "600s"
    max_doublings        = 3
  }

  http_target {
    http_method = "POST"
    uri         = google_cloudfunctions2_function.export.service_config[0].uri

    oidc_token {
      service_account_email = google_service_account.invoker.email
      audience              = google_cloudfunctions2_function.export.service_config[0].uri
    }
  }

  depends_on = [google_cloud_run_service_iam_member.invoker]
}
