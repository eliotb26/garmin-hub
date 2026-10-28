-- One row per day, flattened from the raw Garmin payloads.
DROP VIEW IF EXISTS daily;
DROP VIEW IF EXISTS activities_clean;
CREATE VIEW activities_clean AS
SELECT activity_id, start_local, activity_type,
    json_extract(payload, '$.activityName') AS name,
    ROUND(json_extract(payload, '$.duration') / 60.0) AS minutes,
    ROUND(json_extract(payload, '$.averageHR')) AS avg_hr,
    json_extract(payload, '$.maxHR') AS max_hr,
    ROUND(json_extract(payload, '$.activityTrainingLoad'), 1) AS training_load,
    json_extract(payload, '$.aerobicTrainingEffect') AS aerobic_te,
    json_extract(payload, '$.anaerobicTrainingEffect') AS anaerobic_te,
    payload,
    CASE
        WHEN json_extract(payload, '$.duration') < 180 THEN 'empty'
        WHEN json_extract(payload, '$.maxHR') >= 200 THEN 'hr_artifact'
        WHEN json_extract(payload, '$.duration') > 90 * 60 AND json_extract(payload, '$.averageHR') < 90 THEN 'forgotten_timer'
    END AS quality_flag
FROM activities;

CREATE VIEW daily AS
WITH k AS (
    SELECT day,
        MAX(CASE WHEN kind = 'summary' THEN payload END) AS s,
        MAX(CASE WHEN kind = 'sleep' THEN payload END) AS sl,
        MAX(CASE WHEN kind = 'hrv' THEN payload END) AS h,
        MAX(CASE WHEN kind = 'training_readiness' THEN payload END) AS tr
    FROM daily_raw GROUP BY day
), acts AS (
    SELECT substr(start_local, 1, 10) AS day,
        COUNT(*) AS workouts,
        ROUND(SUM(json_extract(payload, '$.duration')) / 60.0) AS workout_min,
        ROUND(SUM(json_extract(payload, '$.activityTrainingLoad'))) AS training_load,
        group_concat(DISTINCT activity_type) AS workout_types
    FROM activities_clean WHERE quality_flag IS NULL GROUP BY 1
)
SELECT k.day,
    json_extract(s, '$.totalSteps') AS steps,
    json_extract(s, '$.activeKilocalories') AS active_kcal,
    json_extract(s, '$.moderateIntensityMinutes') + 2 * json_extract(s, '$.vigorousIntensityMinutes') AS intensity_min,
    json_extract(s, '$.restingHeartRate') AS resting_hr,
    ROUND((COALESCE(json_extract(s, '$.measurableAwakeDuration'), 0)
         + COALESCE(json_extract(s, '$.measurableAsleepDuration'), 0)) / 3600.0, 1) AS wear_h,
    json_extract(s, '$.averageStressLevel') AS avg_stress,
    json_extract(s, '$.highStressDuration') / 60 AS high_stress_min,
    json_extract(s, '$.bodyBatteryAtWakeTime') AS bb_wake,
    json_extract(s, '$.bodyBatteryHighestValue') AS bb_high,
    json_extract(s, '$.bodyBatteryLowestValue') AS bb_low,
    json_extract(s, '$.bodyBatteryChargedValue') AS bb_charged,
    json_extract(s, '$.bodyBatteryDrainedValue') AS bb_drained,
    ROUND(json_extract(sl, '$.dailySleepDTO.sleepTimeSeconds') / 3600.0, 2) AS sleep_h,
    ROUND(json_extract(sl, '$.dailySleepDTO.deepSleepSeconds') / 60.0) AS deep_min,
    ROUND(json_extract(sl, '$.dailySleepDTO.remSleepSeconds') / 60.0) AS rem_min,
    ROUND(json_extract(sl, '$.dailySleepDTO.napTimeSeconds') / 60.0) AS nap_min,
    json_extract(sl, '$.dailySleepDTO.sleepScores.overall.value') AS sleep_score,
    time(json_extract(sl, '$.dailySleepDTO.sleepStartTimestampLocal') / 1000, 'unixepoch') AS bedtime,
    time(json_extract(sl, '$.dailySleepDTO.sleepEndTimestampLocal') / 1000, 'unixepoch') AS wake_time,
    json_extract(sl, '$.dailySleepDTO.avgSleepStress') AS sleep_stress,
    json_extract(h, '$.hrvSummary.lastNightAvg') AS hrv,
    json_extract(h, '$.hrvSummary.weeklyAvg') AS hrv_7d,
    json_extract(h, '$.hrvSummary.status') AS hrv_status,
    json_extract(tr, '$[0].score') AS readiness,
    json_extract(tr, '$[0].level') AS readiness_level,
    COALESCE(acts.workouts, 0) AS workouts,
    acts.workout_min, acts.training_load, acts.workout_types,
    -- Things a wrist band gets wrong (NULL = clean). Leave flagged days out of averages and correlations.
    NULLIF(trim(
        CASE WHEN (COALESCE(json_extract(s, '$.measurableAwakeDuration'), 0)
                 + COALESCE(json_extract(s, '$.measurableAsleepDuration'), 0)) < 18 * 3600
             THEN 'low_wear ' ELSE '' END ||
        CASE WHEN json_extract(sl, '$.dailySleepDTO.sleepTimeSeconds') > 11 * 3600
               OR time(json_extract(sl, '$.dailySleepDTO.sleepStartTimestampLocal') / 1000, 'unixepoch')
                  BETWEEN '15:00' AND '20:00'
             THEN 'sleep_suspect ' ELSE '' END ||
        CASE WHEN json_extract(sl, '$.dailySleepDTO.sleepTimeSeconds') < 4 * 3600
             THEN 'sleep_short ' ELSE '' END
    ), '') AS quality_flags
FROM k LEFT JOIN acts USING (day)
WHERE json_extract(s, '$.totalSteps') IS NOT NULL
ORDER BY k.day;

-- WHOOP: one row per main (non-nap) sleep, keyed by the day you woke up, so it lines up with `daily`.
DROP VIEW IF EXISTS bands;
DROP VIEW IF EXISTS whoop_daily;
DROP VIEW IF EXISTS whoop_workouts;
CREATE VIEW whoop_daily AS
WITH s AS (
    SELECT record_id AS sleep_id, start_local, end_local, payload AS p
    FROM whoop_raw
    WHERE kind = 'sleep' AND NOT COALESCE(json_extract(payload, '$.nap'), 0)
      AND json_extract(payload, '$.score_state') = 'SCORED'
), r AS (
    SELECT record_id AS sleep_id, payload AS p FROM whoop_raw WHERE kind = 'recovery'
), c AS (
    SELECT record_id AS cycle_id, payload AS p FROM whoop_raw WHERE kind = 'cycle'
), ranked AS (
    -- If two main sleeps end on the same day, keep the longer one.
    SELECT s.*, ROW_NUMBER() OVER (
        PARTITION BY date(s.end_local)
        ORDER BY json_extract(s.p, '$.score.stage_summary.total_in_bed_time_milli') DESC
    ) AS n
    FROM s
)
SELECT date(ranked.end_local) AS day,
    json_extract(r.p, '$.score.recovery_score') AS recovery,
    ROUND(json_extract(r.p, '$.score.hrv_rmssd_milli'), 1) AS hrv,
    json_extract(r.p, '$.score.resting_heart_rate') AS resting_hr,
    json_extract(r.p, '$.score.spo2_percentage') AS spo2,
    json_extract(r.p, '$.score.skin_temp_celsius') AS skin_temp_c,
    json_extract(r.p, '$.score.user_calibrating') AS calibrating,
    ROUND((json_extract(ranked.p, '$.score.stage_summary.total_in_bed_time_milli')
         - json_extract(ranked.p, '$.score.stage_summary.total_awake_time_milli')) / 3600000.0, 2) AS sleep_h,
    ROUND(json_extract(ranked.p, '$.score.stage_summary.total_slow_wave_sleep_time_milli') / 60000.0) AS deep_min,
    ROUND(json_extract(ranked.p, '$.score.stage_summary.total_rem_sleep_time_milli') / 60000.0) AS rem_min,
    json_extract(ranked.p, '$.score.sleep_performance_percentage') AS sleep_performance,
    json_extract(ranked.p, '$.score.sleep_efficiency_percentage') AS sleep_efficiency,
    ROUND(json_extract(ranked.p, '$.score.respiratory_rate'), 1) AS respiratory_rate,
    time(ranked.start_local) AS bedtime,
    time(ranked.end_local) AS wake_time,
    ROUND(json_extract(c.p, '$.score.strain'), 1) AS day_strain,
    -- NULL = clean. Leave flagged days out of averages and correlations.
    NULLIF(trim(
        CASE WHEN json_extract(r.p, '$.score.user_calibrating') THEN 'calibrating ' ELSE '' END ||
        CASE WHEN r.p IS NULL OR json_extract(r.p, '$.score_state') <> 'SCORED' THEN 'no_recovery ' ELSE '' END ||
        CASE WHEN (json_extract(ranked.p, '$.score.stage_summary.total_in_bed_time_milli')
                 - json_extract(ranked.p, '$.score.stage_summary.total_awake_time_milli')) < 4 * 3600000
             THEN 'sleep_short ' ELSE '' END
    ), '') AS quality_flags
FROM ranked
LEFT JOIN r USING (sleep_id)
LEFT JOIN c ON c.cycle_id = CAST(json_extract(ranked.p, '$.cycle_id') AS TEXT)
WHERE ranked.n = 1
ORDER BY day;

CREATE VIEW whoop_workouts AS
SELECT record_id AS workout_id, start_local,
    json_extract(payload, '$.sport_name') AS sport,
    ROUND((julianday(end_local) - julianday(start_local)) * 1440) AS minutes,
    json_extract(payload, '$.score.average_heart_rate') AS avg_hr,
    json_extract(payload, '$.score.max_heart_rate') AS max_hr,
    ROUND(json_extract(payload, '$.score.strain'), 1) AS strain,
    ROUND(json_extract(payload, '$.score.kilojoule') / 4.184) AS kcal,
    json_extract(payload, '$.score.percent_recorded') AS percent_recorded,
    CASE
        WHEN (julianday(end_local) - julianday(start_local)) * 1440 < 3 THEN 'empty'
        WHEN json_extract(payload, '$.score.max_heart_rate') >= 200 THEN 'hr_artifact'
        WHEN json_extract(payload, '$.score.percent_recorded') < 80 THEN 'partial_recording'
    END AS quality_flag
FROM whoop_raw WHERE kind = 'workout' AND json_extract(payload, '$.score_state') = 'SCORED';

-- Garmin and WHOOP side by side for the same night. Different algorithms: compare direction, not raw values.
CREATE VIEW bands AS
SELECT day,
    g.hrv AS garmin_hrv, w.hrv AS whoop_hrv,
    g.resting_hr AS garmin_rhr, w.resting_hr AS whoop_rhr,
    g.sleep_h AS garmin_sleep_h, w.sleep_h AS whoop_sleep_h,
    g.deep_min AS garmin_deep_min, w.deep_min AS whoop_deep_min,
    g.rem_min AS garmin_rem_min, w.rem_min AS whoop_rem_min,
    g.readiness AS garmin_readiness, w.recovery AS whoop_recovery,
    g.bedtime AS garmin_bedtime, w.bedtime AS whoop_bedtime,
    g.quality_flags AS garmin_flags, w.quality_flags AS whoop_flags
FROM daily g JOIN whoop_daily w USING (day)
ORDER BY day;
