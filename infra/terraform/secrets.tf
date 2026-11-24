# Terraform creates the secret *containers* and their access policy, never the
# values: secret versions would otherwise be stored in plain text in the state
# file. Add values out of band (see README):
#
#   printf '%s' "$VALUE" | gcloud secrets versions add <secret-id> --data-file=-

resource "google_secret_manager_secret" "this" {
  for_each = tomap(var.secret_ids)

  project   = var.project_id
  secret_id = each.value

  replication {
    auto {}
  }

  depends_on = [google_project_service.enabled]
}

resource "google_secret_manager_secret_iam_member" "runtime_accessor" {
  for_each = google_secret_manager_secret.this

  project   = var.project_id
  secret_id = each.value.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.runtime.member
}

# WHOOP rotates the refresh token on every use, so the function stores the new
# one as a fresh version. Scoped to that one secret.
resource "google_secret_manager_secret_iam_member" "runtime_version_adder" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.this["refresh_token"].secret_id
  role      = "roles/secretmanager.secretVersionAdder"
  member    = google_service_account.runtime.member
}
