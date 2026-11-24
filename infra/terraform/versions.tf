terraform {
  required_version = ">= 1.6.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }

  # Remote state in a GCS bucket you create once (see README). Supplied at init time:
  #   terraform init -backend-config="bucket=<state-bucket>" -backend-config="prefix=health-data-pipeline"
  backend "gcs" {}
}

provider "google" {
  project = var.project_id
  region  = var.region

  default_labels = var.labels
}
