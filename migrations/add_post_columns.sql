-- Migration: Add post-based creation columns to giveaways, group_giveaways, contests
-- Run this on your PostgreSQL database before restarting the bot.

-- Giveaways
ALTER TABLE giveaways ADD COLUMN IF NOT EXISTS post_text TEXT;
ALTER TABLE giveaways ADD COLUMN IF NOT EXISTS post_file_id VARCHAR(500);
ALTER TABLE giveaways ADD COLUMN IF NOT EXISTS post_media_type VARCHAR(20);
-- Make prize nullable (post itself may contain the prize description)
ALTER TABLE giveaways ALTER COLUMN prize DROP NOT NULL;

-- Group Giveaways
ALTER TABLE group_giveaways ADD COLUMN IF NOT EXISTS post_text TEXT;
ALTER TABLE group_giveaways ADD COLUMN IF NOT EXISTS post_file_id VARCHAR(500);
ALTER TABLE group_giveaways ADD COLUMN IF NOT EXISTS post_media_type VARCHAR(20);
-- Make prize nullable
ALTER TABLE group_giveaways ALTER COLUMN prize DROP NOT NULL;

-- Contests
ALTER TABLE contests ADD COLUMN IF NOT EXISTS post_text TEXT;
ALTER TABLE contests ADD COLUMN IF NOT EXISTS post_file_id VARCHAR(500);
ALTER TABLE contests ADD COLUMN IF NOT EXISTS post_media_type VARCHAR(20);
