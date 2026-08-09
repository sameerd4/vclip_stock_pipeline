"""SQLite schema migrations for the durable VClip catalog."""

MIGRATIONS: tuple[tuple[int, str], ...] = (
    (
        1,
        r"""
        PRAGMA foreign_keys = ON;

        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        );

        CREATE TABLE stockify_runs (
            id TEXT PRIMARY KEY,
            source_xml_path TEXT NOT NULL,
            source_xml_sha256 TEXT NOT NULL,
            source_fcpxml_version TEXT,
            output_xml_path TEXT NOT NULL,
            report_path TEXT NOT NULL,
            manifest_path TEXT,
            pipeline_version TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('running','complete','failed')),
            options_json TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            error_text TEXT
        );

        CREATE TABLE source_events (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES stockify_runs(id) ON DELETE CASCADE,
            source_index INTEGER NOT NULL,
            source_name TEXT NOT NULL,
            source_uid TEXT,
            UNIQUE(run_id, source_index)
        );

        CREATE TABLE shoot_sessions (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES stockify_runs(id) ON DELETE CASCADE,
            session_key TEXT NOT NULL,
            capture_date TEXT,
            captured_at_local TEXT,
            timezone TEXT,
            center_lat REAL,
            center_lon REAL,
            gps_radius_meters REAL,
            country TEXT,
            state TEXT,
            city TEXT,
            neighborhood TEXT,
            poi TEXT,
            public_label TEXT,
            location_confidence TEXT,
            time_of_day TEXT,
            time_of_day_confidence TEXT,
            generated_event_name TEXT NOT NULL,
            generated_base_label TEXT NOT NULL,
            anchor_stock_clip_id TEXT,
            weather_status TEXT NOT NULL DEFAULT 'not_enriched',
            location_json TEXT NOT NULL,
            capture_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(run_id, session_key)
        );

        CREATE TABLE source_projects (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES stockify_runs(id) ON DELETE CASCADE,
            source_event_id TEXT NOT NULL REFERENCES source_events(id) ON DELETE CASCADE,
            source_index INTEGER NOT NULL,
            source_name TEXT NOT NULL,
            source_uid TEXT,
            classification TEXT NOT NULL,
            session_id TEXT REFERENCES shoot_sessions(id),
            anchor_segment_index INTEGER,
            generated_event_name TEXT,
            generated_project_label TEXT,
            generated_compilation_name TEXT,
            accepted_clip_count INTEGER NOT NULL DEFAULT 0,
            skipped_clip_count INTEGER NOT NULL DEFAULT 0,
            sequence_format TEXT,
            tc_format TEXT,
            audio_layout TEXT,
            audio_rate TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(run_id, source_event_id, source_index)
        );

        CREATE TABLE source_media (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES stockify_runs(id) ON DELETE CASCADE,
            asset_ref TEXT,
            asset_name TEXT,
            original_filename TEXT,
            media_path TEXT,
            normalized_stem TEXT,
            duration TEXT,
            duration_seconds REAL,
            format_id TEXT,
            width INTEGER,
            height INTEGER,
            fps INTEGER,
            camera_lut TEXT,
            srt_path TEXT,
            srt_match_method TEXT,
            srt_match_confidence TEXT,
            srt_match_ambiguous INTEGER NOT NULL DEFAULT 0,
            srt_sample_count INTEGER,
            srt_start TEXT,
            srt_end TEXT,
            srt_has_position INTEGER,
            srt_has_altitude INTEGER,
            srt_has_orientation INTEGER,
            captured_at_local TEXT,
            captured_at_utc TEXT,
            capture_date TEXT,
            timezone TEXT,
            location_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(run_id, asset_ref)
        );

        CREATE TABLE stock_candidates (
            run_id TEXT NOT NULL REFERENCES stockify_runs(id) ON DELETE CASCADE,
            stock_clip_id TEXT NOT NULL,
            source_project_id TEXT NOT NULL REFERENCES source_projects(id) ON DELETE CASCADE,
            source_media_id TEXT REFERENCES source_media(id),
            session_id TEXT REFERENCES shoot_sessions(id),
            source_segment_index INTEGER NOT NULL,
            source_ref TEXT,
            source_name TEXT NOT NULL,
            eligibility_status TEXT NOT NULL CHECK(eligibility_status IN ('accepted','rejected')),
            rejection_reason TEXT,
            rejection_detail TEXT,
            original_start TEXT,
            original_duration TEXT,
            original_duration_seconds REAL,
            proposed_start TEXT,
            proposed_duration TEXT,
            proposed_duration_seconds REAL,
            final_start TEXT,
            final_duration TEXT,
            final_duration_seconds REAL,
            review_status TEXT NOT NULL DEFAULT 'pending' CHECK(review_status IN ('pending','approved','rejected','conflict','not_applicable')),
            manually_modified INTEGER NOT NULL DEFAULT 0,
            manual_change_json TEXT NOT NULL DEFAULT '{}',
            short_clip_recovery TEXT,
            candidate_tier TEXT,
            sidecar_path TEXT,
            srt_status TEXT,
            srt_window_status TEXT,
            srt_reasons_json TEXT NOT NULL,
            visual_status TEXT,
            visual_reasons_json TEXT NOT NULL,
            visual_metrics_json TEXT NOT NULL,
            location_json TEXT NOT NULL,
            capture_time_json TEXT NOT NULL,
            time_of_day_json TEXT NOT NULL,
            weather_json TEXT NOT NULL,
            replacement_ref TEXT,
            creative_effects_json TEXT NOT NULL,
            camera_lut TEXT,
            effect_signature TEXT,
            final_effect_signature TEXT,
            generated_event_name TEXT,
            generated_project_label TEXT,
            generated_compilation_name TEXT,
            generated_clip_project_name TEXT,
            clip_sequence INTEGER,
            expected_export_basename TEXT,
            compilation_timeline_offset TEXT,
            project_timecode TEXT,
            export_status TEXT NOT NULL DEFAULT 'pending' CHECK(export_status IN ('pending','matched','missing','not_applicable')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(run_id, stock_clip_id),
            UNIQUE(run_id, source_project_id, source_segment_index)
        );

        CREATE TABLE generated_occurrences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            stock_clip_id TEXT NOT NULL,
            representation TEXT NOT NULL CHECK(representation IN ('compilation','individual')),
            generated_event_name TEXT NOT NULL,
            generated_project_name TEXT NOT NULL,
            project_uid TEXT,
            source_start TEXT NOT NULL,
            duration TEXT NOT NULL,
            timeline_offset TEXT,
            effect_signature TEXT,
            FOREIGN KEY(run_id, stock_clip_id)
                REFERENCES stock_candidates(run_id, stock_clip_id) ON DELETE CASCADE,
            UNIQUE(run_id, stock_clip_id, representation)
        );

        CREATE TABLE reconcile_runs (
            id TEXT PRIMARY KEY,
            stockify_run_id TEXT NOT NULL REFERENCES stockify_runs(id) ON DELETE CASCADE,
            reviewed_xml_path TEXT NOT NULL,
            reviewed_xml_sha256 TEXT NOT NULL,
            authority TEXT NOT NULL,
            scope TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('running','complete','complete_with_conflicts','failed')),
            started_at TEXT NOT NULL,
            completed_at TEXT,
            approved_count INTEGER NOT NULL DEFAULT 0,
            rejected_count INTEGER NOT NULL DEFAULT 0,
            modified_count INTEGER NOT NULL DEFAULT 0,
            conflict_count INTEGER NOT NULL DEFAULT 0,
            report_path TEXT,
            error_text TEXT
        );

        CREATE TABLE review_occurrences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reconcile_run_id TEXT NOT NULL REFERENCES reconcile_runs(id) ON DELETE CASCADE,
            stockify_run_id TEXT NOT NULL,
            stock_clip_id TEXT NOT NULL,
            representation TEXT,
            event_name TEXT,
            project_name TEXT,
            source_start TEXT,
            duration TEXT,
            timeline_offset TEXT,
            effect_signature TEXT,
            FOREIGN KEY(stockify_run_id, stock_clip_id)
                REFERENCES stock_candidates(run_id, stock_clip_id) ON DELETE CASCADE
        );

        CREATE TABLE weather_observations (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES shoot_sessions(id) ON DELETE CASCADE,
            provider TEXT NOT NULL,
            observed_at TEXT,
            fetched_at TEXT NOT NULL,
            status TEXT NOT NULL,
            condition_label TEXT,
            temperature_c REAL,
            precipitation_mm REAL,
            rain_mm REAL,
            cloud_cover_percent REAL,
            visibility_meters REAL,
            wind_speed_kmh REAL,
            weather_code INTEGER,
            raw_json TEXT NOT NULL,
            UNIQUE(session_id, provider)
        );

        CREATE TABLE exports (
            id TEXT PRIMARY KEY,
            stockify_run_id TEXT NOT NULL,
            stock_clip_id TEXT NOT NULL,
            exported_filename TEXT NOT NULL,
            exported_path TEXT NOT NULL,
            match_method TEXT NOT NULL,
            match_confidence TEXT NOT NULL,
            file_size_bytes INTEGER,
            duration_seconds REAL,
            sha256 TEXT,
            reconciled_at TEXT NOT NULL,
            FOREIGN KEY(stockify_run_id, stock_clip_id)
                REFERENCES stock_candidates(run_id, stock_clip_id) ON DELETE CASCADE,
            UNIQUE(stockify_run_id, stock_clip_id),
            UNIQUE(exported_path)
        );

        CREATE TABLE packages (
            id TEXT PRIMARY KEY,
            stockify_run_id TEXT NOT NULL REFERENCES stockify_runs(id) ON DELETE CASCADE,
            source_project_id TEXT NOT NULL REFERENCES source_projects(id) ON DELETE CASCADE,
            session_id TEXT REFERENCES shoot_sessions(id),
            title TEXT NOT NULL,
            slug TEXT NOT NULL,
            output_path TEXT NOT NULL,
            clip_count INTEGER NOT NULL,
            status TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(stockify_run_id, source_project_id)
        );

        CREATE TABLE package_clips (
            package_id TEXT NOT NULL REFERENCES packages(id) ON DELETE CASCADE,
            stockify_run_id TEXT NOT NULL,
            stock_clip_id TEXT NOT NULL,
            export_id TEXT REFERENCES exports(id),
            sort_order INTEGER NOT NULL,
            packaged_filename TEXT NOT NULL,
            packaged_path TEXT NOT NULL,
            PRIMARY KEY(package_id, stock_clip_id),
            FOREIGN KEY(stockify_run_id, stock_clip_id)
                REFERENCES stock_candidates(run_id, stock_clip_id) ON DELETE CASCADE
        );

        CREATE TABLE geocode_cache (
            cache_key TEXT PRIMARY KEY,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            provider TEXT NOT NULL,
            response_json TEXT NOT NULL,
            fetched_at TEXT NOT NULL
        );

        CREATE INDEX idx_candidates_project ON stock_candidates(run_id, source_project_id);
        CREATE INDEX idx_candidates_session ON stock_candidates(run_id, session_id);
        CREATE INDEX idx_candidates_review ON stock_candidates(run_id, review_status);
        CREATE INDEX idx_projects_session ON source_projects(run_id, session_id);
        CREATE INDEX idx_media_stem ON source_media(run_id, normalized_stem);
        """,
    ),
    (
        2,
        r"""
        ALTER TABLE stock_candidates
            ADD COLUMN final_compilation_timeline_offset TEXT;

        ALTER TABLE stock_candidates
            ADD COLUMN final_project_timecode TEXT;
        """,
    ),
    (
        3,
        r"""
        ALTER TABLE source_media
            ADD COLUMN srt_match_candidate_count INTEGER;
        """,
    ),
    (
        4,
        r"""
        CREATE TABLE source_project_families (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES stockify_runs(id) ON DELETE CASCADE,
            session_id TEXT REFERENCES shoot_sessions(id),
            selected_source_project_id TEXT,
            member_count INTEGER NOT NULL,
            similarity_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE INDEX idx_project_families_run
            ON source_project_families(run_id);

        ALTER TABLE source_projects
            ADD COLUMN source_mod_date TEXT;

        ALTER TABLE source_projects
            ADD COLUMN project_family_id TEXT;

        ALTER TABLE source_projects
            ADD COLUMN family_role TEXT;

        ALTER TABLE source_projects
            ADD COLUMN family_selection_reason TEXT;

        ALTER TABLE source_projects
            ADD COLUMN grading_coverage REAL;

        ALTER TABLE source_projects
            ADD COLUMN timeline_signature_json TEXT;
        """,
    ),
    (
        5,
        r"""
        ALTER TABLE weather_observations
            ADD COLUMN requested_at TEXT;

        ALTER TABLE weather_observations
            ADD COLUMN timezone TEXT;

        ALTER TABLE weather_observations
            ADD COLUMN grid_latitude REAL;

        ALTER TABLE weather_observations
            ADD COLUMN grid_longitude REAL;

        ALTER TABLE weather_observations
            ADD COLUMN source_latitude REAL;

        ALTER TABLE weather_observations
            ADD COLUMN source_longitude REAL;
        """,
    ),
    (
        6,
        r"""
        ALTER TABLE shoot_sessions
            ADD COLUMN astronomy_status TEXT NOT NULL DEFAULT 'not_enriched';

        CREATE TABLE astronomy_observations (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL UNIQUE REFERENCES shoot_sessions(id) ON DELETE CASCADE,
            status TEXT NOT NULL,
            sunrise_time TEXT,
            sunset_time TEXT,
            minutes_from_sunrise INTEGER,
            minutes_from_sunset INTEGER,
            solar_period TEXT,
            timezone TEXT,
            source_latitude REAL,
            source_longitude REAL,
            concept_signals_json TEXT NOT NULL,
            visual_analysis_json TEXT NOT NULL,
            computed_at TEXT NOT NULL,
            raw_json TEXT NOT NULL
        );
        """,
    ),
    (
        7,
        r"""
        CREATE TABLE processed_libraries (
            id TEXT PRIMARY KEY,
            library_name TEXT NOT NULL,
            library_path TEXT NOT NULL UNIQUE,
            first_stockify_run_id TEXT NOT NULL REFERENCES stockify_runs(id),
            last_stockify_run_id TEXT NOT NULL REFERENCES stockify_runs(id),
            first_processed_at TEXT NOT NULL,
            last_processed_at TEXT NOT NULL
        );

        CREATE INDEX idx_processed_libraries_name
            ON processed_libraries(library_name);
        """,
    ),
)
