# -*- coding: utf-8 -*-
import streamlit as st
import pandas as pd
import os
import plotly.express as px

# ==========================================
# 1. 全局样式与页面深度定制
# ==========================================
st.set_page_config(
    page_title="MCN 商业价值调度舱", 
    page_icon="📊", 
    layout="wide", 
    initial_sidebar_state="collapsed"
)

hide_st_style = """
<style>
#MainMenu {visibility: hidden;}
footer {visibility: hidden;}
header {visibility: hidden;}
.block-container {padding-top: 2rem; padding-bottom: 2rem; max-width: 95%;}
div[data-testid="stMetricValue"] {font-size: 1.8rem; font-weight: 700; color: #1E3A8A;}
</style>
"""
st.markdown(hide_st_style, unsafe_allow_html=True)

# ==========================================
# 2. 数据处理与清洗引擎
# ==========================================
DATA_FILE = "weiq_results.xlsx"

# 智能中文单位换算器（解决尾数带 .0 的不美观问题）
def format_chinese_unit(num):
    if pd.isna(num):
        return "0"
    try:
        val = float(num)
        if val >= 100000000:
            res = val / 100000000
            return f"{int(res)}亿" if res == int(res) else f"{res:.2f}亿"
        elif val >= 10000:
            res = val / 10000
            return f"{int(res)}万" if res == int(res) else f"{res:.1f}万"
        else:
            return f"{int(val)}"
    except:
        return str(num)

@st.cache_data(ttl=60)
def load_data():
    if not os.path.exists(DATA_FILE):
        return pd.DataFrame()
    
    try:
        df = pd.read_excel(DATA_FILE)
        
        def clean_number(x):
            if isinstance(x, str):
                x = x.replace(',', '').replace(' ', '').replace('¥', '').replace('￥', '')
                if '万' in x:
                    try:
                        return float(x.replace('万', '')) * 10000
                    except ValueError:
                        return None
                try:
                    return float(x)
                except ValueError:
                    pass
            return pd.to_numeric(x, errors='coerce')

        core_numeric_cols = ["粉丝数", "直发CPM", "阅读中位数", "发布博文数"]
        for col in core_numeric_cols:
            if col in df.columns:
                df[f"{col}_数值"] = df[col].apply(clean_number)
                
        return df
    except Exception as e:
        st.error(f"数据读取异常: {e}")
        return pd.DataFrame()

# ==========================================
# 3. 极简图表渲染函数 (修复倒序与0值异常)
# ==========================================
def create_clean_bar_chart(df, x_col, y_col, title, color_hex, max_items=20, sort_ascending=True):
    # 【核心修复】：彻底剔除 0 值或负值（尤其是 CPM 为 0 的废数据）
    df_valid = df[df[x_col] > 0].copy()

    # 智能截取 Top N 保护内存
    if len(df_valid) > max_items:
        if sort_ascending:
            df_plot = df_valid.nlargest(max_items, x_col).copy() # 越高越好的指标取最大值
        else:
            df_plot = df_valid.nsmallest(max_items, x_col).copy() # CPM这种越低越好的指标取最小值
        title_suffix = f" (截取 Top {max_items})"
    else:
        df_plot = df_valid.copy()
        title_suffix = ""

    # Plotly横向柱状图排版逻辑：若要最好的在最上面，需要调整 ascending 参数
    df_sorted = df_plot.sort_values(by=x_col, ascending=sort_ascending)
    df_sorted['文本标签'] = df_sorted[x_col].apply(format_chinese_unit)
    
    fig = px.bar(
        df_sorted, 
        x=x_col, 
        y=y_col, 
        orientation='h', 
        text='文本标签'
    )
    
    dynamic_height = max(250, len(df_plot) * 35 + 80)
    
    fig.update_traces(marker_color=color_hex, textfont_size=12, textposition="outside", cliponaxis=False)
    fig.update_layout(
        title=dict(text=title + title_suffix, font=dict(size=14, color="#333333")),
        plot_bgcolor='rgba(0,0,0,0)',
        paper_bgcolor='rgba(0,0,0,0)',
        xaxis=dict(showgrid=False, showticklabels=False, title=''),
        yaxis=dict(title='', showgrid=False, tickfont=dict(size=12, color="#4B5563")),
        margin=dict(l=0, r=60, t=40, b=0),
        height=dynamic_height,
        hovermode="y"
    )
    return fig

# ==========================================
# 4. 主程序：全局调度与联动视图
# ==========================================
def main():
    st.title("MCN 商业数据分析大盘")
    st.markdown("---")

    df = load_data()

    if df.empty:
        st.warning("系统提示：未检测到有效数据，请确认底表状态。")
        return

    valid_df = df.dropna(subset=["粉丝数_数值"])

    # ==========================================
    # 商业级矩阵结构热力树图 (Treemap)
    # ==========================================
    st.markdown("### 一、 大盘资产结构热力图")
    st.markdown("<span style='color:gray; font-size: 0.9em;'>注：区块面积代表【粉丝体量】，颜色代表【CPM成本】（<span style='color:#10B981;'>绿色</span>为低成本优质，<span style='color:#EF4444;'>红色</span>为高成本风险）。</span>", unsafe_allow_html=True)
    
    if "直发CPM_数值" in df.columns and "阅读中位数_数值" in df.columns and "粉丝数_数值" in df.columns:
        macro_df = df.dropna(subset=["直发CPM_数值", "阅读中位数_数值", "粉丝数_数值"]).copy()
        
        # 过滤0值脏数据，防止计算中位数和热力图扭曲
        macro_df = macro_df[(macro_df["粉丝数_数值"] > 0) & (macro_df["直发CPM_数值"] > 0)]
        
        if not macro_df.empty:
            median_cpm = macro_df["直发CPM_数值"].median()
            median_read = macro_df["阅读中位数_数值"].median()
            
            # 【全新注入】：提前格式化好所有要在热力图里显示的中文单位数据
            macro_df["阅读量_展示"] = macro_df["阅读中位数_数值"].apply(format_chinese_unit)
            macro_df["粉丝数_展示"] = macro_df["粉丝数_数值"].apply(format_chinese_unit)

            def get_quadrant(row):
                if row["阅读中位数_数值"] >= median_read and row["直发CPM_数值"] < median_cpm:
                    return "高阅读 · 低CPM (优质)"
                elif row["阅读中位数_数值"] >= median_read and row["直发CPM_数值"] >= median_cpm:
                    return "高阅读 · 高CPM"
                elif row["阅读中位数_数值"] < median_read and row["直发CPM_数值"] < median_cpm:
                    return "低阅读 · 低CPM"
                else:
                    return "低阅读 · 高CPM (劣势)"
                    
            macro_df["数据表现"] = macro_df.apply(get_quadrant, axis=1)
            
            # 【全新配色方案】：引入"绿->黄->红"红绿灯色彩心理模型
            # 低CPM = #10B981 (翠绿)，中位CPM = #FBBF24 (琥珀黄)，高CPM = #EF4444 (警示红)
            fig_treemap = px.treemap(
                macro_df,
                path=[px.Constant("全量资产大盘"), "数据表现", "账号ID"],
                values="粉丝数_数值",
                color="直发CPM_数值",
                color_continuous_scale=["#10B981", "#FBBF24", "#EF4444"], 
                color_continuous_midpoint=median_cpm,
                custom_data=["直发CPM_数值", "阅读量_展示", "粉丝数_展示", "数据表现"],
                title=f"矩阵结构分布 (CPM中位数基准: {median_cpm:.1f})"
            )
            
            # 【核心修复】：彻底清理 Hover 杂质，植入纯净的 Tooltip 和区块文本
            fig_treemap.update_traces(
                texttemplate="<b>%{label}</b><br>%{customdata[2]}", # 区块内直观显示账号名+中文化粉丝数
                hovertemplate="<b>%{label}</b><br>数据表现=%{customdata[3]}<br>直发 CPM (成本)=%{customdata[0]:.2f}<br>阅读中位数 (曝光)=%{customdata[1]}<br>粉丝数_数值=%{customdata[2]}<extra></extra>", # 去掉多余代码残余
                marker=dict(line=dict(color='#FFFFFF', width=1.5))
            )
            
            fig_treemap.update_layout(
                margin=dict(t=40, l=10, r=10, b=10),
                height=500
            )
            st.plotly_chart(fig_treemap, use_container_width=True)

    st.markdown("---")
    st.markdown("### 二、 核心数据精准对标")
    
    display_cols = [c for c in df.columns if "_数值" not in c]
    df_display = df[display_cols].copy()

    selection_event = st.dataframe(
        df_display,
        use_container_width=True,
        height=300,
        on_select="rerun",
        selection_mode="multi-row"
    )

    selected_indices = selection_event.selection.rows

    st.markdown("---")
    
    # ==========================================
    # 动态对比视图
    # ==========================================
    if not selected_indices:
        st.info("系统提示：尚未在列表中勾选账号。当前默认展示粉丝数 Top 10 账号。")
        selected_df = valid_df.copy()
    else:
        selected_df = df.iloc[selected_indices].copy()
        if len(selected_df) > 20:
            st.warning(f"由于您勾选了 {len(selected_df)} 个账号，为保证图表可读性，系统已自动提取各项指标的 Top 20 进行对比。")
        else:
            st.success(f"已锁定 {len(selected_df)} 个账号，对标视图如下：")

    if not selected_df.empty:
        row1_col1, row1_col2 = st.columns(2)
        row2_col1, row2_col2 = st.columns(2)
        
        # 排序策略：数值越大越好的传 True，数值越小越好的传 False
        with row1_col1:
            if "粉丝数_数值" in selected_df.columns:
                fig1 = create_clean_bar_chart(
                    selected_df, "粉丝数_数值", "账号ID", 
                    "粉丝总数", "#1E40AF", sort_ascending=True
                )
                st.plotly_chart(fig1, use_container_width=True, config={'displayModeBar': False})
                
        with row1_col2:
            if "直发CPM_数值" in selected_df.columns:
                fig2 = create_clean_bar_chart(
                    selected_df, "直发CPM_数值", "账号ID", 
                    "直发 CPM (成本优先)", "#0D9488", sort_ascending=False  # CPM越低越好，传 False
                )
                st.plotly_chart(fig2, use_container_width=True, config={'displayModeBar': False})

        st.markdown("<br>", unsafe_allow_html=True)

        with row2_col1:
            if "阅读中位数_数值" in selected_df.columns:
                fig3 = create_clean_bar_chart(
                    selected_df, "阅读中位数_数值", "账号ID", 
                    "阅读中位数", "#4F46E5", sort_ascending=True
                )
                st.plotly_chart(fig3, use_container_width=True, config={'displayModeBar': False})
                
        with row2_col2:
            if "发布博文数_数值" in selected_df.columns:
                fig4 = create_clean_bar_chart(
                    selected_df, "发布博文数_数值", "账号ID", 
                    "近30天发布博文数", "#7C3AED", sort_ascending=True
                )
                st.plotly_chart(fig4, use_container_width=True, config={'displayModeBar': False})

if __name__ == "__main__":
    main()