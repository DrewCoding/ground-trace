terraform {
    required_version = "~> 1.0"
    required_providers{
        aws = {
            source = "hashicorp/aws"
            version = "~> 6.0"
        }

        archive = {
            source = "hashicorp/archive"
            version = "~> 2.0"
        }
    }

    # State lives in S3 rather than on one laptop. The bucket is created
    # out-of-band (see README) because a bucket cannot sensibly bootstrap the
    # state file that describes it.
    #
    # use_lockfile enables S3-native locking via conditional writes, which
    # replaced the old DynamoDB lock table. dynamodb_table is deprecated as
    # of Terraform 1.11 and slated for removal.
    #
    # Backend blocks cannot reference variables - these have to be literals.
    backend "s3" {
        bucket       = "ground-trace-tfstate-970208041269"
        key          = "aws/terraform.tfstate"
        region       = "us-west-1"
        encrypt      = true
        use_lockfile = true
    }
}

provider "aws" {
  region = var.aws_region
}
