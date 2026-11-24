# Three single-purpose identities instead of the default compute service
# account (which has Editor on the whole project):
#
#   runtime   - what the function runs as: reads its secrets, rotates the
#               refresh token, writes to the Drive folder shared with it.
#   invoker   - what Cloud Scheduler signs its OIDC token as; may only invoke.
#   build     - what Cloud Build uses to turn the source zip into a container.

resource "google_service_account" "runtime" {
  account_id   = "${var.name}-run"
  display_name = "WHOOP exporter runtime"
  description  = "Runs the ${var.name} Cloud Function. Share the destination Drive folder with this account."

  depends_on = [google_project_service.enabled]
}

resource "google_service_account" "invoker" {
  account_id   = "${var.name}-sched"
  display_name = "WHOOP exporter scheduler"
  description  = "Identity Cloud Scheduler uses to call the ${var.name} function."

  depends_on = [google_project_service.enabled]
}

resource "google_service_account" "build" {
  account_id   = "${var.name}-build"
  display_name = "WHOOP exporter build"
  description  = "Cloud Build identity for building the ${var.name} function."

  depends_on = [google_project_service.enabled]
}

resource "google_project_iam_member" "build" {
  for_each = toset([
    "roles/cloudbuild.builds.builder",
  ])

  project = var.project_id
  role    = each.value
  member  = google_service_account.build.member
}

# Only the invoker may call the function; it is not publicly reachable.
resource "google_cloud_run_service_iam_member" "invoker" {
  project  = var.project_id
  location = google_cloudfunctions2_function.export.location
  service  = google_cloudfunctions2_function.export.name
  role     = "roles/run.invoker"
  member   = google_service_account.invoker.member
}
