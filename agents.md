# agents.md

Источник правды по продукту:

- [глоссарий](docs/glossary.md)
- [модель](docs/domain.md)
- [потоки](docs/flows.md)
- [вне MVP](docs/out-of-scope.md)

Агент не меняет эти правила и не додумывает продукт от себя. Если задача с ними расходится, он останавливается и сообщает оркестратору.

Техника и порядок работ — в [плане](docs/plan.md):

- [стек](docs/plan.md#stack)
- [первый checkpoint](docs/plan.md#checkpoint-1)
- [исполнение первого checkpoint](docs/plan.md#checkpoint-1-build)
- [чанки первого checkpoint](docs/plan.md#checkpoint-1-chunks)
- [десктоп](docs/plan.md#desktop)
- [следующие вехи](docs/plan.md#later)

Агент не подбирает другой стек и не переставляет эти рамки. Если задача с планом расходится, он останавливается и сообщает оркестратору.

Порядок работы — в [конвейере](docs/pipeline.md). Инструкции ролей:

- [оркестратор](docs/agents/orchestrator.md)
- [воркер](docs/agents/worker.md)
- [ревьюер](docs/agents/reviewer.md)
- [документатор](docs/agents/documenter.md)

Агент следует своей роли. Чужую роль не подменяет. С владельцем продукта говорит оркестратор.
