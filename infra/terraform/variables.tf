variable "project_id" {
  description = "GCP project that hosts the exporter. A dedicated project is recommended."
  type        = string
}

variable "region" {
  description = "Region for the Cloud Function, Cloud Scheduler job and source bucket."
  type        = string
  default     = "us-central1"
}

variable "name" {
  description = "Base name for the function, service accounts and scheduler job."
  type        = string
  default     = "whoop-export"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{3,22}[a-z0-9]$", var.name))
    error_message = "name must be 5-24 lowercase letters, digits or hyphens (it prefixes service account ids)."
  }
}

variable "gdrive_folder_id" {
  description = "Drive folder (in a Shared Drive) the function writes exports into. Not a secret, but keep it out of git: pass it via TF_VAR_gdrive_folder_id or an untracked .tfvars file."
  type        = string

  validation {
    condition     = length(trimspace(var.gdrive_folder_id)) > 0
    error_message = "gdrive_folder_id must not be empty."
  }
}

variable "days_back" {
  description = "Days of history fetched per run (DAYS_BACK)."
  type        = number
  default     = 7

  validation {
    condition     = var.days_back >= 1 && var.days_back <= 365 && floor(var.days_back) == var.days_back
    error_message = "days_back must be a whole number between 1 and 365 (the same range the function accepts)."
  }
}

variable "schedule" {
  description = "Cron schedule for the daily export."
  type        = string
  default     = "0 9 * * *"
}

variable "time_zone" {
  description = "IANA time zone the schedule is evaluated in."
  type        = string
  default     = "Etc/UTC"
}

variable "source_archive_path" {
  description = "Zip produced by `python -m tools.package_function` (path relative to this directory, or absolute)."
  type        = string
  default     = "../../dist/function-source.zip"
}

variable "secret_ids" {
  description = "Secret Manager secret ids. The function reads all three and adds versions to refresh_token (WHOOP rotates refresh tokens)."
  type = object({
    client_id     = string
    client_secret = string
    refresh_token = string
  })
  default = {
    client_id     = "whoop-client-id"
    client_secret = "whoop-client-secret"
    refresh_token = "whoop-refresh-token"
  }
}

variable "source_retention_days" {
  description = "Delete uploaded source archives older than this. The running revision keeps its own copy, so old archives only matter for rollbacks."
  type        = number
  default     = 90
}

variable "github_repository" {
  description = "owner/repo allowed to deploy through Workload Identity Federation (keyless GitHub Actions). null skips the CI deployer entirely."
  type        = string
  default     = null

  validation {
    condition     = var.github_repository == null || can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.github_repository))
    error_message = "github_repository must look like owner/repo."
  }
}

variable "labels" {
  description = "Labels applied to every resource that supports them."
  type        = map(string)
  default = {
    app        = "health-data-pipeline"
    managed-by = "terraform"
  }
}
