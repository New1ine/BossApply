import unittest
from xml.etree.ElementTree import fromstring

from bossapply.engine.list_state import job_list_state


def page(contents="", *, adjacent=""):
    return fromstring(
        '<node package="com.hpbr.bosszhipin" bounds="[0,0][500,1000]">'
        '<node resource-id="id/refresh_layout" bounds="[0,100][500,900]">'
        '<node resource-id="id/rv_list" bounds="[0,100][500,900]">'
        + contents + '</node></node>' + adjacent + '</node>'
    )


class ListStateTests(unittest.TestCase):
    def test_idle_refresh_container_does_not_prove_loading_or_readiness(self):
        state = job_list_state(page())
        self.assertFalse(state.known)
        self.assertFalse(state.loading)
        self.assertFalse(state.end)
        self.assertFalse(state.retry)

    def test_missing_job_list_is_unknown(self):
        self.assertFalse(job_list_state(fromstring('<node/>')).known)

    def test_visible_progress_widget_in_list_is_loading(self):
        state = job_list_state(page('<node class="android.widget.ProgressBar" bounds="[210,800][290,880]"/>'))
        self.assertTrue(state.loading)
        self.assertTrue(state.known)

    def test_adjacent_footer_progress_is_loading(self):
        state = job_list_state(page(adjacent=(
            '<node resource-id="id/load_more_footer" bounds="[0,880][500,960]">'
            '<node class="android.widget.ProgressBar" bounds="[210,895][290,950]"/></node>'
        )))
        self.assertTrue(state.loading)

    def test_job_description_and_decorative_progress_are_not_list_status(self):
        state = job_list_state(page(
            '<node clickable="true" bounds="[0,650][500,900]">'
            '<node resource-id="id/tv_position_name" text="加载中" bounds="[20,670][250,710]"/>'
            '<node resource-id="id/job_requirements" text="没有更多了" bounds="[20,720][250,760]"/>'
            '<node class="android.widget.ProgressBar" bounds="[20,780][50,810]"/>'
            '</node>'
        ))
        self.assertFalse(state.known)

    def test_hidden_and_zero_area_progress_are_ignored(self):
        state = job_list_state(page(
            '<node class="android.widget.ProgressBar" visible-to-user="false" bounds="[20,800][60,850]"/>'
            '<node class="android.widget.ProgressBar" bounds="[0,0][0,0]"/>'
        ))
        self.assertFalse(state.loading)

    def test_progress_inside_a_hidden_status_container_is_ignored(self):
        state = job_list_state(page(
            '<node resource-id="id/loading" displayed="false" bounds="[0,750][500,900]">'
            '<node class="android.widget.ProgressBar" bounds="[20,800][60,850]"/></node>'
        ))
        self.assertFalse(state.loading)

    def test_footer_loading_label_is_recognized_but_idle_load_more_is_not(self):
        loading = job_list_state(page('<node text="正在加载更多…" bounds="[100,820][400,870]"/>'))
        self.assertTrue(loading.loading)
        for label in ("点击加载更多", "上拉加载更多", "加载更多"):
            with self.subTest(label=label):
                self.assertFalse(job_list_state(page(f'<node text="{label}" bounds="[100,820][400,870]"/>')).loading)

    def test_footer_end_label_is_recognized(self):
        for label in ("没有更多了", "没有更多职位了", "已经到底了", "暂无更多职位"):
            with self.subTest(label=label):
                state = job_list_state(page(f'<node text="{label}" bounds="[100,820][400,870]"/>'))
                self.assertTrue(state.end)
                self.assertFalse(state.loading)

    def test_retry_label_may_be_a_clickable_footer(self):
        state = job_list_state(page(
            '<node resource-id="id/load_error" bounds="[0,700][500,900]">'
            '<node clickable="true" text="加载失败，点击重试" bounds="[100,760][400,840]"/></node>'
        ))
        self.assertTrue(state.retry)
        self.assertFalse(state.end)

    def test_network_error_status_is_recognized(self):
        state = job_list_state(page('<node resource-id="id/error_message" text="网络异常，请重试" bounds="[100,450][400,520]"/>'))
        self.assertTrue(state.retry)

    def test_unrelated_progress_outside_list_region_is_ignored(self):
        state = job_list_state(page(adjacent='<node class="android.widget.ProgressBar" bounds="[0,0][60,30]"/>'))
        self.assertFalse(state.known)

    def test_loading_takes_precedence_over_a_stale_end_or_error_label(self):
        state = job_list_state(page(
            '<node class="android.widget.ProgressBar" bounds="[210,740][290,810]"/>'
            '<node text="没有更多了" bounds="[100,820][400,850]"/>'
            '<node text="点击重试" bounds="[100,860][400,890]"/>'
        ))
        self.assertTrue(state.loading)
        self.assertFalse(state.end)
        self.assertFalse(state.retry)

    def test_tokens_are_comparable_and_change_when_status_changes(self):
        loading = page('<node text="加载中" bounds="[100,820][400,870]"/>')
        self.assertEqual(job_list_state(loading).token, job_list_state(loading).token)
        end = page('<node text="没有更多了" bounds="[100,820][400,870]"/>')
        self.assertNotEqual(job_list_state(loading).token, job_list_state(end).token)


if __name__ == "__main__":
    unittest.main()
