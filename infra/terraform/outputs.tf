output "function_name" {
  description = "Lambda function name, for manual invocation and log lookups"
  value       = aws_lambda_function.poller.function_name
}

output "function_arn" {
  value = aws_lambda_function.poller.arn
}

output "ecr_repository_url" {
  description = "Push the poller image here before applying a new image_tag"
  value       = aws_ecr_repository.poller.repository_url
}

output "role_arn" {
  description = "Execution role. Grant this inbound access on the RDS security group."
  value       = aws_iam_role.poller.arn
}

output "log_group" {
  value = aws_cloudwatch_log_group.poller.name
}

output "invoke_poll_command" {
  description = "Run a cycle by hand"
  value = join(" ", [
    "aws lambda invoke",
    "--function-name ${aws_lambda_function.poller.function_name}",
    "--payload '{\"action\":\"poll\"}'",
    "--cli-binary-format raw-in-base64-out",
    "/dev/stdout",
  ])
}

output "backfill_command" {
  description = "Seed history without alerting on any of it"
  value = join(" ", [
    "aws lambda invoke",
    "--function-name ${aws_lambda_function.poller.function_name}",
    "--payload '{\"action\":\"backfill\",\"days\":180}'",
    "--cli-binary-format raw-in-base64-out",
    "/dev/stdout",
  ])
}
