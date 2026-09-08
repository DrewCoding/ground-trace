module vpc{
    source  = "terraform-aws-modules/vpc/aws"
    version = "6.4.0"

    name = "ground-trace-vpc"
    cidr = var.vpc_cidr
    private_subnets = var.private_subnet_cidrs
    public_subnets = var.public_subnet_cidrs
    azs = slice(data.aws_availability_zones.available.names, 0, 2)
    enable_nat_gateway = false
    single_nat_gateway = false
    one_nat_gateway_per_az = false
}
