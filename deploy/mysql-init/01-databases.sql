CREATE DATABASE IF NOT EXISTS waygent_checkpoints CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin;
GRANT ALL PRIVILEGES ON waygent_checkpoints.* TO 'waygent'@'%';
FLUSH PRIVILEGES;
