output "db_url" {
  description = "通用 serverless 数据库:DataGrip 风格 web 控制台(浏览器打开)"
  value       = aws_lambda_function_url.db.function_url
}

output "db_ro_url" {
  description = "只读 endpoint:随便并发,给读多的程序用(写请求会被 403)"
  value       = aws_lambda_function_url.db_ro.function_url
}

output "demo_url" {
  description = "示例业务服务"
  value       = aws_lambda_function_url.demo.function_url
}

output "db_bucket" {
  value = aws_s3_bucket.db.bucket
}

output "db_prefix" {
  description = "库文件前缀:s3://<bucket>/<prefix><db>.sqlite"
  value       = local.prefix
}
