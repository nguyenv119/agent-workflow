// Harbor replacement for test/global-setup.ts: same migrations + seed + per-pool clone, but against a
// local Postgres started by test.sh (no Docker/Testcontainers available in the verifier).
import { fileURLToPath } from 'node:url'
import { dirname, join, resolve } from 'node:path'
import { existsSync, readdirSync } from 'node:fs'
import { execa } from 'execa'
import type { ProvidedContext } from 'vitest'
import { integrationTestWorkerCount } from './integration-workers.js'

const here = dirname(fileURLToPath(import.meta.url))
const packageRoot = resolve(here, '..')
const migrationsDir = resolve(packageRoot, 'prisma/migrations')
const seedEntry = resolve(packageRoot, 'prisma/seed.ts')
const PG = 'postgresql://postgres@127.0.0.1:5432'

interface GlobalSetupContext {
  provide<K extends keyof ProvidedContext & string>(key: K, value: ProvidedContext[K]): void
}

const psql = (sql: string) => execa('psql', ['-v', 'ON_ERROR_STOP=1', `${PG}/postgres`, '-c', sql], { stdio: 'inherit' })

export async function setup({ provide }: GlobalSetupContext): Promise<void> {
  const workers = integrationTestWorkerCount()
  for (let i = 1; i <= workers; i += 1) await psql(`DROP DATABASE IF EXISTS "test_worker_${i}"`)
  await psql('DROP DATABASE IF EXISTS "test"')
  await psql('CREATE DATABASE "test"')
  const url = `${PG}/test`
  // Prisma's native schema engine is unavailable offline, so apply the committed migrations in order.
  const migrations = readdirSync(migrationsDir, { withFileTypes: true })
    .filter((d) => d.isDirectory() && existsSync(join(migrationsDir, d.name, 'migration.sql')))
    .map((d) => d.name)
    .sort()
  for (const name of migrations) {
    await execa('psql', ['-q', '-v', 'ON_ERROR_STOP=1', url, '-f', join(migrationsDir, name, 'migration.sql')], {
      stdio: ['ignore', 'ignore', 'inherit']
    })
  }
  await execa('pnpm', ['exec', 'tsx', '--conditions', 'development', seedEntry], {
    cwd: packageRoot,
    env: { ...process.env, DATABASE_URL: url },
    stdio: 'inherit'
  })
  const urls: string[] = []
  for (let i = 1; i <= workers; i += 1) {
    await psql(`CREATE DATABASE "test_worker_${i}" WITH TEMPLATE "test"`)
    urls.push(`${PG}/test_worker_${i}`)
  }
  provide('databaseUrls', urls)
}

export async function teardown(): Promise<void> {}

declare module 'vitest' {
  export interface ProvidedContext {
    databaseUrls: string[]
  }
}
