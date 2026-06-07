"""用真实 qstat -f 样本验证解析器，重点覆盖折行拼接。"""
from pathlib import Path

from app.pbs.parser import parse_jobs, parse_qstat_f, parse_variable_list

FIXTURE = Path(__file__).parent / "fixtures" / "qstat_f_1785.txt"


def _raw():
    return parse_qstat_f(FIXTURE.read_text())[0]


def test_basic_fields():
    f = _raw()
    assert f["Job Id"] == "1785.hpcmaster"
    assert f["Job_Name"] == "JACD32poleDRPA2"
    assert f["job_state"] == "R"
    assert f["queue"] == "batch"
    assert f["euser"] == "user07"


def test_folded_path_reassembled():
    """Error_Path 在 'c' 后折行，必须拼回 case2 而非 c ase2。"""
    f = _raw()
    assert (
        f["Error_Path"]
        == "hpcmaster:/data/project/user07/ZXY/JAC_D_side/32_pole/DR_PA/case2/JACD32poleDRPA2.e1785"
    )
    assert "c ase2" not in f["Error_Path"]


def test_variable_list_workdir():
    f = _raw()
    vlist = parse_variable_list(f["Variable_List"])
    assert (
        vlist["PBS_O_WORKDIR"]
        == "/data/project/user07/ZXY/JAC_D_side/32_pole/DR_PA/case2"
    )
    # INITDIR 折行处也要拼对（ca + se2 -> case2）
    assert vlist["PBS_O_INITDIR"].endswith("DR_PA/case2")
    assert vlist["PBS_O_HOST"] == "hpcmaster"


def test_to_job():
    job = parse_jobs(FIXTURE.read_text())[0]
    assert job.short_id == "1785"
    assert job.owner == "user07"
    assert job.state == "R"
    assert job.workdir == "/data/project/user07/ZXY/JAC_D_side/32_pole/DR_PA/case2"
    assert job.exec_host == "hpcnode5.upcloud.local/0-63"
    assert job.walltime_used == "17:57:47"
    assert job.walltime_limit == "99:00:00"
    assert job.nodes == "1:ppn=64"
    assert job.submit_ts is not None  # qtime 解析成功


if __name__ == "__main__":
    import sys

    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failed += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if failed else 0)
