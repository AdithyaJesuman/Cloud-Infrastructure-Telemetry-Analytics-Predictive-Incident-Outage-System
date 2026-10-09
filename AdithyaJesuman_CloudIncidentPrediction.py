import os
import sys
import warnings
warnings.filterwarnings('ignore')
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import seaborn as sns
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import RandomForestClassifier, HistGradientBoostingClassifier, IsolationForest
from sklearn.metrics import accuracy_score, precision_score, recall_score, f1_score, roc_auc_score, confusion_matrix
import joblib

DATA_PATH = os.path.join(os.path.dirname(__file__), 'data', 'cloud_telemetry_outages.csv')
ASSETS_DIR = os.path.join(os.path.dirname(__file__), 'assets')

def load_data(path=DATA_PATH):
    if not os.path.exists(path):
        fallback = os.path.join('data', 'cloud_telemetry_outages.csv')
        if os.path.exists(fallback):
            path = fallback
        else:
            raise FileNotFoundError(f"Dataset not found at {path}")
    df = pd.read_csv(path)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df = df.sort_values('timestamp').reset_index(drop=True)
    return df

def engineer_features(df):
    data = df.copy()
    metrics = ['cpu_percent', 'response_time_ms', 'queue_depth', 'error_rate', 'db_query_time_ms']
    for m in metrics:
        data[f'{m}_roll_mean_3'] = data[m].rolling(window=3, min_periods=1).mean()
        data[f'{m}_roll_std_3'] = data[m].rolling(window=3, min_periods=1).std().fillna(0)
        data[f'{m}_lag_1'] = data[m].shift(1).bfill()
        data[f'{m}_diff_1'] = data[m] - data[f'{m}_lag_1']
    data['cpu_memory_ratio'] = data['cpu_percent'] / (data['memory_percent'] + 1e-5)
    data['queue_to_throughput'] = data['queue_depth'] / (data['throughput_rps'] + 1.0)
    data['latency_db_ratio'] = data['response_time_ms'] / (data['db_query_time_ms'] + 1.0)
    
    outage_condition = (
        (data['error_rate'] > 500000.0) |
        (data['response_time_ms'] > 3000.0) |
        (data['queue_depth'] > 5000000.0)
    ).astype(int)
    
    data['incident_lead'] = outage_condition.rolling(window=4, min_periods=1).max().shift(-3).fillna(0).astype(int)
    return data

def get_train_test_splits(df):
    feature_cols = [
        'cpu_percent', 'memory_percent', 'response_time_ms', 'error_rate',
        'active_connections', 'throughput_rps', 'queue_depth', 'db_query_time_ms',
        'cpu_percent_roll_mean_3', 'cpu_percent_roll_std_3', 'cpu_percent_diff_1',
        'response_time_ms_roll_mean_3', 'response_time_ms_roll_std_3', 'response_time_ms_diff_1',
        'queue_depth_roll_mean_3', 'queue_depth_diff_1',
        'error_rate_roll_mean_3', 'error_rate_diff_1',
        'cpu_memory_ratio', 'queue_to_throughput', 'latency_db_ratio'
    ]
    target_col = 'incident_lead'
    split_index = int(len(df) * 0.8)
    train_df = df.iloc[:split_index]
    test_df = df.iloc[split_index:]
    X_train = train_df[feature_cols]
    y_train = train_df[target_col]
    X_test = test_df[feature_cols]
    y_test = test_df[target_col]
    return X_train, X_test, y_train, y_test, feature_cols

def train_and_evaluate(X_train, X_test, y_train, y_test, feature_cols):
    models = {
        'Logistic Regression': Pipeline([
            ('scaler', StandardScaler()),
            ('clf', LogisticRegression(max_iter=2000, random_state=42))
        ]),
        'Random Forest': RandomForestClassifier(n_estimators=100, max_depth=10, random_state=42, class_weight='balanced'),
        'HistGradientBoosting': HistGradientBoostingClassifier(max_iter=100, max_depth=8, random_state=42)
    }
    results = {}
    trained_models = {}
    
    for name, model in models.items():
        model.fit(X_train, y_train)
        preds = model.predict(X_test)
        proba = model.predict_proba(X_test)[:, 1] if hasattr(model, 'predict_proba') else preds
        results[name] = {
            'accuracy': float(accuracy_score(y_test, preds)),
            'precision': float(precision_score(y_test, preds, zero_division=0)),
            'recall': float(recall_score(y_test, preds, zero_division=0)),
            'f1': float(f1_score(y_test, preds, zero_division=0)),
            'roc_auc': float(roc_auc_score(y_test, proba)),
            'confusion_matrix': confusion_matrix(y_test, preds),
            'predictions': preds,
            'probabilities': proba
        }
        trained_models[name] = model
        
    iso = IsolationForest(contamination=0.08, random_state=42)
    iso.fit(X_train)
    iso_preds = (iso.predict(X_test) == -1).astype(int)
    results['Isolation Forest'] = {
        'accuracy': float(accuracy_score(y_test, iso_preds)),
        'precision': float(precision_score(y_test, iso_preds, zero_division=0)),
        'recall': float(recall_score(y_test, iso_preds, zero_division=0)),
        'f1': float(f1_score(y_test, iso_preds, zero_division=0)),
        'roc_auc': float(roc_auc_score(y_test, iso_preds)),
        'confusion_matrix': confusion_matrix(y_test, iso_preds),
        'predictions': iso_preds,
        'probabilities': iso_preds
    }
    trained_models['Isolation Forest'] = iso
    return results, trained_models

def export_figures(df, results, trained_models, feature_cols, X_test, y_test):
    os.makedirs(ASSETS_DIR, exist_ok=True)
    sns.set_theme(style='whitegrid', font='sans-serif')
    
    fig, axes = plt.subplots(3, 1, figsize=(14, 10), sharex=True)
    axes[0].plot(df['timestamp'], df['cpu_percent'], color='#1f77b4', linewidth=1.2)
    axes[0].set_ylabel('CPU Utilization (%)')
    axes[0].set_title('Telemetry Metrics & Severe Outage Spikes Over Time', fontsize=14, fontweight='bold')
    
    axes[1].plot(df['timestamp'], df['response_time_ms'], color='#ff7f0e', linewidth=1.2)
    axes[1].set_ylabel('Latency (ms)')
    
    axes[2].plot(df['timestamp'], df['queue_depth'], color='#d62728', linewidth=1.2)
    axes[2].set_ylabel('Queue Depth')
    axes[2].set_xlabel('Timestamp')
    
    plt.tight_layout()
    plt.savefig(os.path.join(ASSETS_DIR, 'eda_telemetry_trends.png'), dpi=200)
    plt.close()
    
    corr_cols = ['cpu_percent', 'memory_percent', 'response_time_ms', 'error_rate', 'queue_depth', 'db_query_time_ms', 'incident_lead']
    corr = df[corr_cols].corr()
    plt.figure(figsize=(9, 7))
    sns.heatmap(corr, annot=True, fmt='.2f', cmap='Blues', cbar=True, square=True)
    plt.title('Correlation Matrix of Core Infrastructure Metrics', fontsize=13, fontweight='bold')
    plt.tight_layout()
    plt.savefig(os.path.join(ASSETS_DIR, 'correlation_heatmap.png'), dpi=200)
    plt.close()
    
    metrics_summary = []
    for model_name, res in results.items():
        metrics_summary.append({
            'Model': model_name,
            'Accuracy': res['accuracy'],
            'Precision': res['precision'],
            'Recall': res['recall'],
            'F1-Score': res['f1'],
            'ROC-AUC': res['roc_auc']
        })
    res_df = pd.DataFrame(metrics_summary)
    
    plt.figure(figsize=(10, 5))
    bar_df = res_df.melt(id_vars='Model', value_vars=['Precision', 'Recall', 'F1-Score'], var_name='Metric', value_name='Score')
    sns.barplot(data=bar_df, x='Model', y='Score', hue='Metric', palette='mako')
    plt.title('Model Benchmark: Precision, Recall & F1-Score', fontsize=13, fontweight='bold')
    plt.ylim(0, 1.05)
    plt.tight_layout()
    plt.savefig(os.path.join(ASSETS_DIR, 'model_comparison.png'), dpi=200)
    plt.close()
    
    best_cm = results['Random Forest']['confusion_matrix']
    plt.figure(figsize=(6, 5))
    sns.heatmap(best_cm, annot=True, fmt='d', cmap='Blues', cbar=False,
                xticklabels=['Normal', 'Incident Risk'], yticklabels=['Normal', 'Incident Risk'])
    plt.xlabel('Predicted Label', fontweight='bold')
    plt.ylabel('Actual Label', fontweight='bold')
    plt.title('Random Forest Confusion Matrix (Holdout Set)', fontsize=12, fontweight='bold')
    plt.tight_layout()
    plt.savefig(os.path.join(ASSETS_DIR, 'confusion_matrix.png'), dpi=200)
    plt.close()
    
    rf = trained_models['Random Forest']
    importances = rf.feature_importances_
    fi_df = pd.DataFrame({'feature': feature_cols, 'importance': importances}).sort_values('importance', ascending=False).head(10)
    plt.figure(figsize=(9, 5))
    sns.barplot(data=fi_df, x='importance', y='feature', hue='feature', palette='viridis', legend=False)
    plt.title('Top 10 Incident Predictor Drivers', fontsize=13, fontweight='bold')
    plt.xlabel('Gini Feature Importance', fontweight='bold')
    plt.tight_layout()
    plt.savefig(os.path.join(ASSETS_DIR, 'feature_importance.png'), dpi=200)
    plt.close()

def run_pipeline():
    raw_df = load_data()
    featured_df = engineer_features(raw_df)
    X_train, X_test, y_train, y_test, feature_cols = get_train_test_splits(featured_df)
    results, models = train_and_evaluate(X_train, X_test, y_train, y_test, feature_cols)
    export_figures(featured_df, results, models, feature_cols, X_test, y_test)
    os.makedirs(os.path.join(os.path.dirname(__file__), 'models'), exist_ok=True)
    joblib.dump(models['Random Forest'], os.path.join(os.path.dirname(__file__), 'models', 'rf_incident_model.pkl'))
    joblib.dump(feature_cols, os.path.join(os.path.dirname(__file__), 'models', 'feature_cols.pkl'))
    return featured_df, results, models, feature_cols

def render_dashboard():
    import streamlit as st
    st.set_page_config(page_title="Cloud Telemetry & Incident Prediction Dashboard", layout="wide")
    st.title("Cloud Infrastructure Telemetry & Incident Prediction Dashboard")
    st.markdown("Automated Site Reliability Analytics and Outage Risk Forecasting")
    
    featured_df, results, models, feature_cols = run_pipeline()
    rf_model = models['Random Forest']
    
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Monitored Intervals", f"{len(featured_df):,}")
    col2.metric("Incident Events", f"{int(featured_df['incident_lead'].sum()):,}")
    col3.metric("F1 Detection Score", f"{results['Random Forest']['f1']:.3f}")
    col4.metric("ROC-AUC Benchmark", f"{results['Random Forest']['roc_auc']:.3f}")
    
    st.markdown("---")
    
    tab1, tab2, tab3 = st.tabs(["Telemetry Overview", "Model Performance", "Live Outage Predictor"])
    
    with tab1:
        st.subheader("Historical Infrastructure Telemetry")
        st.line_chart(featured_df.set_index('timestamp')[['cpu_percent', 'response_time_ms', 'db_query_time_ms']])
        
        st.subheader("High Pressure Correlations")
        if os.path.exists(os.path.join(ASSETS_DIR, 'correlation_heatmap.png')):
            st.image(os.path.join(ASSETS_DIR, 'correlation_heatmap.png'))
            
    with tab2:
        st.subheader("Model Evaluation Summary")
        summary_rows = []
        for name, res in results.items():
            summary_rows.append({
                "Model": name,
                "Accuracy": f"{res['accuracy']:.4f}",
                "Precision": f"{res['precision']:.4f}",
                "Recall": f"{res['recall']:.4f}",
                "F1-Score": f"{res['f1']:.4f}",
                "ROC-AUC": f"{res['roc_auc']:.4f}"
            })
        st.dataframe(pd.DataFrame(summary_rows), use_container_width=True)
        
        col_m1, col_m2 = st.columns(2)
        with col_m1:
            if os.path.exists(os.path.join(ASSETS_DIR, 'model_comparison.png')):
                st.image(os.path.join(ASSETS_DIR, 'model_comparison.png'))
        with col_m2:
            if os.path.exists(os.path.join(ASSETS_DIR, 'confusion_matrix.png')):
                st.image(os.path.join(ASSETS_DIR, 'confusion_matrix.png'))
                
    with tab3:
        st.subheader("Interactive Incident Risk Scoring")
        st.markdown("Provide real-time telemetry inputs to evaluate impending outage risk.")
        
        c1, c2, c3 = st.columns(3)
        input_cpu = c1.slider("CPU Utilization (%)", min_value=10.0, max_value=100.0, value=65.0, step=0.5)
        input_mem = c2.slider("Memory Utilization (%)", min_value=10.0, max_value=100.0, value=48.0, step=0.5)
        input_lat = c3.slider("Response Time (ms)", min_value=50.0, max_value=5000.0, value=1200.0, step=10.0)
        
        c4, c5, c6 = st.columns(3)
        input_err = c4.number_input("Error Rate Metric", value=150000.0, step=10000.0)
        input_qd = c5.number_input("Queue Depth", value=750000.0, step=50000.0)
        input_db = c6.number_input("DB Query Time (ms)", value=45.0, step=1.0)
        
        sample_input = pd.DataFrame([{
            'cpu_percent': input_cpu,
            'memory_percent': input_mem,
            'response_time_ms': input_lat,
            'error_rate': input_err,
            'active_connections': 185.0,
            'throughput_rps': 3500.0,
            'queue_depth': input_qd,
            'db_query_time_ms': input_db,
            'cpu_percent_roll_mean_3': input_cpu,
            'cpu_percent_roll_std_3': 2.1,
            'cpu_percent_diff_1': 3.5,
            'response_time_ms_roll_mean_3': input_lat,
            'response_time_ms_roll_std_3': 150.0,
            'response_time_ms_diff_1': 200.0,
            'queue_depth_roll_mean_3': input_qd,
            'queue_depth_diff_1': 50000.0,
            'error_rate_roll_mean_3': input_err,
            'error_rate_diff_1': 10000.0,
            'cpu_memory_ratio': input_cpu / (input_mem + 1e-5),
            'queue_to_throughput': input_qd / 3501.0,
            'latency_db_ratio': input_lat / (input_db + 1.0)
        }])[feature_cols]
        
        prob = rf_model.predict_proba(sample_input)[0, 1]
        
        st.write("")
        if prob >= 0.70:
            st.error(f"HIGH CRITICAL OUTAGE RISK: {prob * 100:.1f}%. Immediate scaling / circuit-breaker mitigation required.")
        elif prob >= 0.35:
            st.warning(f"MODERATE ELEVATED RISK: {prob * 100:.1f}%. Traffic queue growth detected; monitor upstream dependencies.")
        else:
            st.success(f"NOMINAL CLUSTER STATUS: {prob * 100:.1f}%. Infrastructure operating inside healthy bounds.")

if __name__ == '__main__':
    is_streamlit = os.environ.get('STREAMLIT_RUNNING') or 'streamlit' in sys.argv[0]
    if len(sys.argv) > 1 and sys.argv[1] == '--dashboard':
        render_dashboard()
    elif is_streamlit:
        render_dashboard()
    else:
        df, results, models, f_cols = run_pipeline()
        print("Pipeline execution finished successfully.")
        for k, v in results.items():
            print(f"{k} -> Accuracy: {v['accuracy']:.4f}, Precision: {v['precision']:.4f}, Recall: {v['recall']:.4f}, F1: {v['f1']:.4f}, ROC-AUC: {v['roc_auc']:.4f}")
