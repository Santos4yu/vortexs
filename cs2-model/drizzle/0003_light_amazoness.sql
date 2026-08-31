CREATE TABLE `cs2_player_map_logs` (
	`id` integer PRIMARY KEY AUTOINCREMENT NOT NULL,
	`source` text NOT NULL,
	`source_map_id` text NOT NULL,
	`match_key` text NOT NULL,
	`played_at` text NOT NULL,
	`player` text NOT NULL,
	`player_key` text NOT NULL,
	`team` text DEFAULT '' NOT NULL,
	`opponent` text DEFAULT '' NOT NULL,
	`map_number` integer NOT NULL,
	`map_name` text DEFAULT 'Unknown' NOT NULL,
	`kills` integer NOT NULL,
	`headshots` integer,
	`rounds` integer NOT NULL,
	`source_url` text NOT NULL,
	`imported_at` text DEFAULT CURRENT_TIMESTAMP NOT NULL
);
--> statement-breakpoint
CREATE INDEX `idx_cs2_map_logs_player_date` ON `cs2_player_map_logs` (`player_key`,`played_at`);--> statement-breakpoint
CREATE INDEX `idx_cs2_map_logs_match_player` ON `cs2_player_map_logs` (`match_key`,`player_key`);