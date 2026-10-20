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
