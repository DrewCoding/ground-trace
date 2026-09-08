variable "aws_region" {
    type = string
    default = "us-west-1"
}

variable "vpc_cidr"{
    type = string
    default = "10.0.0.0/16"
}

data "aws_availability_zones" "available" {
    state = "available"
}

variable "public_subnet_cidrs"{
    type = list(string)
    default = ["10.0.1.0/24", "10.0.2.0/24"]
}

variable "private_subnet_cidrs"{
    type = list(string)
    default = ["10.0.101.0/24", "10.0.102.0/24"]
}

variable "project_name" {
    type = string
    default = "ground-trace"
}
