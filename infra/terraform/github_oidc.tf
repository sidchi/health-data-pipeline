# Keyless deploys from GitHub Actions via Workload Identity Federation: no
# service account key ever exists. Only workflows in var.github_repository can
# exchange their OIDC token, and only for the deployer account.
#
# Bootstrap order: an owner runs the first `terraform apply` locally with
# github_repository set; after that the Deploy workflow manages the stack.

locals {
  github_enabled = var.github_repository != null

  # The deployer manages everything in this stack, including IAM, so it is
  # inherently privileged. Keep the stack in its own project and gate applies
  # behind a protected GitHub environment.
  deployer_roles = local.github_enabled ? toset([
    "roles/cloudfunctions.admin",
    "roles/cloudscheduler.admin",
    "roles/iam.serviceAccountAdmin",
    "roles/iam.serviceAccountUser",
    "roles/iam.workloadIdentityPoolAdmin",
    "roles/resourcemanager.projectIamAdmin",
    "roles/run.admin",
    "roles/secretmanager.admin",
    "roles/serviceusage.serviceUsageAdmin",
    "roles/storage.admin",
  ]) : toset([])
}

resource "google_iam_workload_identity_pool" "github" {
  count = local.github_enabled ? 1 : 0

  project                   = var.project_id
  workload_identity_pool_id = "github"
  display_name              = "GitHub Actions"
  description               = "OIDC tokens issued to GitHub Actions workflows."

  depends_on = [google_project_service.enabled]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  count = local.github_enabled ? 1 : 0

  project                            = var.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.github[0].workload_identity_pool_id
  workload_identity_pool_provider_id = "github-actions"
  display_name                       = "GitHub Actions"

  attribute_mapping = {
    "google.subject"             = "assertion.sub"
    "attribute.repository"       = "assertion.repository"
    "attribute.repository_owner" = "assertion.repository_owner"
    "attribute.ref"              = "assertion.ref"
  }

  # Reject tokens from every other repository before any IAM check runs.
  attribute_condition = "assertion.repository == \"${var.github_repository}\""

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account" "deployer" {
  count = local.github_enabled ? 1 : 0

  account_id   = "${var.name}-deploy"
  display_name = "WHOOP exporter deployer (GitHub Actions)"
  description  = "Assumed by ${var.github_repository} workflows to plan and apply this stack."

  depends_on = [google_project_service.enabled]
}

resource "google_service_account_iam_member" "deployer_wif" {
  count = local.github_enabled ? 1 : 0

  service_account_id = google_service_account.deployer[0].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github[0].name}/attribute.repository/${var.github_repository}"
}

resource "google_project_iam_member" "deployer" {
  for_each = local.deployer_roles

  project = var.project_id
  role    = each.value
  member  = google_service_account.deployer[0].member
}
