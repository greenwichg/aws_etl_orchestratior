###############################################################################
# QA Environment - Outputs (pass-through from root module)
###############################################################################

output "s3_bucket_name" {
  value = module.etl_pipeline.s3_bucket_name
}

output "redshift_workgroup_name" {
  value = module.etl_pipeline.redshift_workgroup_name
}

output "redshift_database" {
  value = module.etl_pipeline.redshift_database
}

output "redshift_secret_arn" {
  value = module.etl_pipeline.redshift_secret_arn
}

output "redshift_endpoint" {
  value = module.etl_pipeline.redshift_endpoint
}

output "lambda_orchestrator_arn" {
  value = module.etl_pipeline.lambda_orchestrator_arn
}

output "state_machine_arn" {
  value = module.etl_pipeline.state_machine_arn
}

output "glue_simple_job_name" {
  value = module.etl_pipeline.glue_simple_job_name
}

output "glue_data_model_job_name" {
  value = module.etl_pipeline.glue_data_model_job_name
}

output "sns_topic_arn" {
  value = module.etl_pipeline.sns_topic_arn
}

output "sqs_queue_url" {
  value = module.etl_pipeline.sqs_queue_url
}

output "sqs_dlq_url" {
  value = module.etl_pipeline.sqs_dlq_url
}
