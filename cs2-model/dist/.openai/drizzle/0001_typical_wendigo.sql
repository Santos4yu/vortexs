CREATE INDEX `idx_cs2_evaluations_created_at` ON `cs2_evaluations` (`created_at`);--> statement-breakpoint
CREATE INDEX `idx_cs2_evaluations_result_date` ON `cs2_evaluations` (`result`,`event_date`);