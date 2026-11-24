resource "google_storage_bucket" "source" {
  project  = var.project_id
  name     = "${var.project_id}-${var.name}-source"
  location = var.region

  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = true

  lifecycle_rule {
    condition {
      age = var.source_retention_days
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.enabled]
}

resource "google_storage_bucket_object" "source" {
  name   = local.source_object
  bucket = google_storage_bucket.source.name
  source = var.source_archive_path
}

resource "google_cloudfunctions2_function" "export" {
  project     = var.project_id
  location    = var.region
  name        = var.name
  description = "Daily WHOOP export to Google Drive (source ${substr(local.source_sha256, 0, 12)})."

  build_config {
    runtime         = "python312"
    entry_point     = "trigger_upload"
    service_account = google_service_account.build.id

    source {
      storage_source {
        bucket = google_storage_bucket.source.name
        object = google_storage_bucket_object.source.name
      }
    }
  }

  service_config {
    # One run a day: a single instance avoids two concurrent runs racing to
    # rotate the WHOOP refresh token. Ingress stays ALLOW_ALL because Cloud
    # Scheduler calls the public URL; IAM still demands an OIDC token.
    min_instance_count               = 0
    max_instance_count               = 1
    max_instance_request_concurrency = 1
    available_memory                 = "256M"
    timeout_seconds                  = 300
    ingress_settings                 = "ALLOW_ALL"
    all_traffic_on_latest_revision   = true
    service_account_email            = google_service_account.runtime.email

    environment_variables = {
      GDRIVE_FOLDER_ID = var.gdrive_folder_id
      DAYS_BACK        = tostring(var.days_back)
      WHOOP_SECRET_ID  = google_secret_manager_secret.this["refresh_token"].secret_id
    }

    secret_environment_variables {
      key        = "WHOOP_CLIENT_ID"
      project_id = var.project_id
      secret     = google_secret_manager_secret.this["client_id"].secret_id
      version    = "latest"
    }

    secret_environment_variables {
      key        = "WHOOP_CLIENT_SECRET"
      project_id = var.project_id
      secret     = google_secret_manager_secret.this["client_secret"].secret_id
      version    = "latest"
    }
  }

  depends_on = [
    google_project_iam_member.build,
    google_secret_manager_secret_iam_member.runtime_accessor,
  ]
}
