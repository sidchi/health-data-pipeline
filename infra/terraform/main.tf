locals {
  services = [
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "cloudfunctions.googleapis.com",
    "cloudscheduler.googleapis.com",
    "drive.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "storage.googleapis.com",
    "sts.googleapis.com",
  ]

  # Object name changes only when the (deterministic) archive changes, so an
  # unchanged source tree plans as a no-op instead of a redeploy.
  source_sha256 = filesha256(var.source_archive_path)
  source_object = "${var.name}/${local.source_sha256}.zip"
}

resource "google_project_service" "enabled" {
  for_each = toset(local.services)

  project = var.project_id
  service = each.value

  # Disabling APIs on destroy can break unrelated resources in the project.
  disable_on_destroy = false
}
