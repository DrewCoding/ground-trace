resource "aws_dynamodb_table" "sessions" {
    name = "${var.project_name}-sessions"
    billing_mode = "PAY_PER_REQUEST"
    hash_key = "sessionId"

    attribute {
        name = "sessionId"
        type = "S"
    }

    attribute {
        name = "status"
        type = "S"
    }

    attribute {
        name = "createdAt"
        type = "N"
    }

    global_secondary_index {
        name = "status-createdAt-index"
        hash_key = "status"
        range_key = "createdAt"
        projection_type = "ALL"
    }

    ttl {
        attribute_name = "expiresAt"
        enabled = true
    }

    point_in_time_recovery {
        enabled = false
    }

    tags = {
        Project = var.project_name
    }
}