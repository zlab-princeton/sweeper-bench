"""browser-use evaluation harness. The judge reads the agent's history."""


def build_agent(*, task, llm, browser, **kwargs):
    from browser_use import Agent

    return Agent(
        task=task,
        llm=llm,
        browser=browser,
        llm_timeout=kwargs.get("llm_timeout"),
        step_timeout=kwargs.get("step_timeout"),
    )
