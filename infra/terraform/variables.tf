variable "aws_region" {
  type    = string
  default = "ap-south-1"
}

variable "vpc_id" {
  type = string
}

variable "admin_cidrs" {
  type    = list(string)
  default = ["0.0.0.0/0"]
}

