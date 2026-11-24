output "function_uri" {
  description = "HTTPS endpoint of the function (requires an OIDC token from the invoker account)."
  value       = google_cloudfunctions2_function.export.service_config[0].uri
}

output "runtime_service_account" {
  description = "Share the destination Drive folder with this account as Content manager."
  value       = google_service_account.runtime.email
}

output "scheduler_job" {
  description = "Run it on demand with: gcloud scheduler jobs run <name> --location <region>."
  value       = google_cloud_scheduler_job.daily.name
}

output "secret_ids" {
  description = "Secrets to populate with `gcloud secrets versions add` before the first run."
  value       = { for key, secret in google_secret_manager_secret.this : key => secret.secret_id }
}

output "source_object" {
  description = "Uploaded source archive (named by its SHA-256)."
  value       = "gs://${google_storage_bucket.source.name}/${google_storage_bucket_object.source.name}"
}

output "github_workload_identity_provider" {
  description = "Set as the GCP_WIF_PROVIDER repository variable (null when github_repository is unset)."
  value       = local.github_enabled ? google_iam_workload_identity_pool_provider.github[0].name : null
}

output "github_deployer_service_account" {
  description = "Set as the GCP_DEPLOY_SA repository variable (null when github_repository is unset)."
  value       = local.github_enabled ? google_service_account.deployer[0].email : null
}
