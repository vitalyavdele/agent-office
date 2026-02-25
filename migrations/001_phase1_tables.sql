-- Phase 1: Agent Office v2 — новые таблицы
-- Выполнить в Supabase SQL Editor: https://supabase.com/dashboard/project/njhbitemndotsfxxwzat/sql

-- Память агентов
CREATE TABLE IF NOT EXISTS agent_memory (
    id            BIGSERIAL PRIMARY KEY,
    agent         TEXT NOT NULL,
    memory_type   TEXT NOT NULL,       -- 'lesson', 'fact', 'preference', 'skill'
    content       TEXT NOT NULL,
    source_task_id BIGINT,
    importance    SMALLINT DEFAULT 5,  -- 1-10
    usage_count   INT DEFAULT 0,
    tags          TEXT[] DEFAULT '{}',
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

-- Профиль пользователя
CREATE TABLE IF NOT EXISTS user_profile (
    id         BIGSERIAL PRIMARY KEY,
    category   TEXT NOT NULL,            -- 'style', 'preference', 'habit'
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    confidence REAL DEFAULT 0.5,
    source     TEXT,                     -- 'explicit', 'inferred', 'feedback'
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(category, key)
);

-- Feedback по задачам
CREATE TABLE IF NOT EXISTS task_feedback (
    id         BIGSERIAL PRIMARY KEY,
    task_id    BIGINT,
    agent      TEXT NOT NULL,
    rating     SMALLINT,                -- 1-5
    comment    TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Ошибки + рефлексия
CREATE TABLE IF NOT EXISTS agent_errors (
    id           BIGSERIAL PRIMARY KEY,
    agent        TEXT NOT NULL,
    task_id      BIGINT,
    error_type   TEXT NOT NULL,
    error_detail TEXT NOT NULL,
    reflection   TEXT,
    lesson       TEXT,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);

-- Идеи (миграция из in-memory)
CREATE TABLE IF NOT EXISTS ideas (
    id         BIGSERIAL PRIMARY KEY,
    content    TEXT NOT NULL,
    status     TEXT DEFAULT 'planning',
    plan_text  TEXT,
    result     TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Статьи (миграция из in-memory)
CREATE TABLE IF NOT EXISTS articles (
    id            BIGSERIAL PRIMARY KEY,
    title         TEXT NOT NULL,
    content       TEXT NOT NULL,
    published_url TEXT,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

-- Расширение существующей таблицы tasks
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS assigned_agent TEXT;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS priority TEXT DEFAULT 'normal';
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS tags TEXT[] DEFAULT '{}';
