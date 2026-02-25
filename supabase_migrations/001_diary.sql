-- diary: лог всех событий агентов
create table if not exists diary (
    id         bigint generated always as identity primary key,
    agent      text not null,
    event_type text not null default 'status_change',
    content    text not null default '',
    created_at timestamptz not null default now()
);

create index if not exists idx_diary_agent      on diary (agent);
create index if not exists idx_diary_created_at  on diary (created_at desc);

-- RLS
alter table diary enable row level security;
create policy "anon read diary"  on diary for select using (true);
create policy "anon insert diary" on diary for insert with check (true);


-- scheduled_tasks: задачи с горизонтами планирования
create table if not exists scheduled_tasks (
    id         bigint generated always as identity primary key,
    title      text not null,
    horizon    text not null check (horizon in ('now', 'day', 'week', 'month')),
    priority   text not null default 'normal' check (priority in ('urgent', 'normal', 'later')),
    status     text not null default 'pending' check (status in ('pending', 'in_progress', 'done', 'cancelled')),
    created_at timestamptz not null default now(),
    updated_at timestamptz
);

create index if not exists idx_sched_horizon on scheduled_tasks (horizon);
create index if not exists idx_sched_status  on scheduled_tasks (status);

-- RLS
alter table scheduled_tasks enable row level security;
create policy "anon read scheduled_tasks"   on scheduled_tasks for select using (true);
create policy "anon insert scheduled_tasks" on scheduled_tasks for insert with check (true);
create policy "anon update scheduled_tasks" on scheduled_tasks for update using (true);
