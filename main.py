from agent import Agent365Agent
from host import AgentHost


if __name__ == "__main__":
    host = AgentHost(Agent365Agent)
    host.run()
