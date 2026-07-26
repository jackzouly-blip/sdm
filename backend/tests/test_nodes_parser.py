"""验证 pbsnodes -a 输出解析：状态归一、核槽/作业统计、status 负载解析。"""
from app.nodes.pbsnodes import parse_pbsnodes

SAMPLE = """node01
     state = free
     power_state = Running
     np = 64
     ntype = cluster
     status = opsys=linux,loadave=0.05,ncpus=64,physmem=131072000kb,availmem=120000000kb,totmem=135000000kb
     mom_service_port = 15002
     gpus = 0

node02
     state = job-exclusive
     power_state = Running
     np = 64
     ntype = cluster
     jobs = 0/1785.hpcmaster,1/1785.hpcmaster,2/1786.hpcmaster
     status = opsys=linux,loadave=63.9,ncpus=64

node03
     state = down,offline
     np = 64
     ntype = cluster

node04
     state = job-exclusive
     np = 64
     jobs = 0-63/2364.hpcmaster
     status = opsys=linux,loadave=64.00,ncpus=128
"""


def test_node_count_and_names():
    nodes = parse_pbsnodes(SAMPLE)
    assert [n["name"] for n in nodes] == ["node01", "node02", "node03", "node04"]


def test_free_node_health_and_status():
    n = parse_pbsnodes(SAMPLE)[0]
    assert n["health"] == "up"
    assert n["np"] == 64
    assert n["used_slots"] == 0
    assert n["running_jobs"] == 0
    assert n["loadave"] == "0.05"
    assert n["ncpus"] == 64


def test_jobs_slots_and_unique_jobids():
    n = parse_pbsnodes(SAMPLE)[1]
    # 3 个核槽项，去重后 2 个作业（1785 / 1786）
    assert n["used_slots"] == 3
    assert n["running_jobs"] == 2
    assert n["health"] == "up"  # job-exclusive 视为正常在用


def test_down_offline_health():
    n = parse_pbsnodes(SAMPLE)[2]
    assert n["states"] == ["down", "offline"]
    assert n["health"] == "down"  # 含 down 优先判为宕机
    # mom 未上报 status 时负载/内存缺失
    assert n["loadave"] is None
    assert n["availmem"] is None


def test_exclusive_node_range_slots():
    """独占节点 jobs 用范围写法 `0-63/jid`，应算 64 核而非 1。"""
    n = parse_pbsnodes(SAMPLE)[3]
    assert n["used_slots"] == 64
    assert n["running_jobs"] == 1
    assert n["health"] == "up"


def test_empty_input():
    assert parse_pbsnodes("") == []
