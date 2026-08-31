CREATE TABLE `board_imports` (
	`id` integer PRIMARY KEY AUTOINCREMENT NOT NULL,
	`source` text NOT NULL,
	`prop_count` integer NOT NULL,
	`payload_json` text NOT NULL,
	`status` text DEFAULT 'confirmed' NOT NULL,
	`created_at` text DEFAULT CURRENT_TIMESTAMP NOT NULL
);
--> statement-breakpoint
CREATE TABLE `cs2_evaluations` (
	`id` integer PRIMARY KEY AUTOINCREMENT NOT NULL,
	`import_id` integer,
	`event_date` text NOT NULL,
	`player` text NOT NULL,
	`team` text NOT NULL,
	`opponent` text NOT NULL,
	`market` text NOT NULL,
	`line` real NOT NULL,
	`side` text NOT NULL,
	`projection` real NOT NULL,
	`probability` real NOT NULL,
	`edge_pp` real NOT NULL,
	`data_confidence` integer NOT NULL,
	`tier` text NOT NULL,
	`qualified` integer NOT NULL,
	`model_version` text NOT NULL,
	`evidence_json` text NOT NULL,
	`warnings_json` text NOT NULL,
	`actual` real,
	`result` text DEFAULT 'pending' NOT NULL,
	`created_at` text DEFAULT CURRENT_TIMESTAMP NOT NULL,
	`graded_at` text
);
