# One command runs everything: `make test` (needs only Docker).
COMPOSE := docker compose
RUN     := $(COMPOSE) run --rm runner
PYTEST  := python -m pytest -p no:cacheprovider

.PHONY: test up seed lint selftest rules perf ai demo validate draft-rule ai-eval down

test: lint seed selftest rules perf ai demo   ## full suite, no manual steps

up:            ## start PostgreSQL and wait until it is healthy
	$(COMPOSE) up -d --wait db

seed: up       ## seed the planted database and the clean one
	$(RUN) python db/seed.py
	$(RUN) python db/seed.py --clean --dbname theracare_clean

lint:          ## ruff, zero warnings
	$(RUN) ruff check .

selftest:      ## each rule catches exactly its planted rows; clean data passes every rule
	$(RUN) $(PYTEST) tests/test_selftest.py --html=reports/selftest.html --self-contained-html

rules:         ## the rule suite against the clean database (the green CI gate)
	$(COMPOSE) run --rm -e PGDATABASE=theracare_clean runner \
		$(PYTEST) tests/test_rules.py --html=reports/theracare_clean/report.html --self-contained-html

perf:          ## EXPLAIN ANALYZE the five heaviest rules; required indexes exist
	$(RUN) $(PYTEST) tests/test_performance.py -s

ai:            ## AI guard-rail tests (offline); live AI tests run only with ANTHROPIC_API_KEY
	$(RUN) $(PYTEST) tests/test_ai.py

demo:          ## the rule suite against the planted database: failures expected, reports written
	-$(RUN) $(PYTEST) tests/test_rules.py --html=reports/theracare/report.html --self-contained-html

validate:      ## CLI run, e.g. make validate ARGS="--severity critical"
	$(RUN) python -m framework.runner $(ARGS)

draft-rule:    ## AI rule draft, e.g. make draft-rule STORY="Providers see at most 8 patients a day"
	$(RUN) python -m framework.ai.rule_drafter "$(STORY)"

ai-eval:       ## AI output evaluation against the golden notes
	$(RUN) python -m framework.ai.evaluation

down:          ## stop containers and drop the data volume
	$(COMPOSE) down -v
