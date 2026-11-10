"""Entrenamiento de modelos predictivos - MODULO 4 (KDD).

Lee el dataset modelable, preprocesa las variables operativas del siniestro
(incluye calle_1 y es_post_semaforos) y optimiza tres modelos con
GridSearchCV (StratifiedKFold de 5 folds, refit por F1):

    1) DecisionTreeClassifier  (interpretable)
    2) RandomForestClassifier  (ensamble bagging)
    3) GradientBoostingClassifier (ensamble boosting)

Salidas:
    data/modelo_riesgo.pkl      -> pipelines serializados (joblib)
    data/metricas_modelos.json  -> mejores hiperparametros, metricas en test,
                                   matrices de confusion y feature importance
                                   para /api/metricas-ml

Uso:
    python src/entrenar_modelos.py
"""

import json
from datetime import datetime
from pathlib import Path

import joblib
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import (GradientBoostingClassifier,
                              RandomForestClassifier)
from sklearn.impute import SimpleImputer
from sklearn.metrics import (accuracy_score, confusion_matrix, f1_score,
                             precision_score, recall_score)
from sklearn.model_selection import (GridSearchCV, StratifiedKFold,
                                     train_test_split)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder
from sklearn.tree import DecisionTreeClassifier

RAIZ = Path(__file__).resolve().parent.parent
RUTA_CSV = RAIZ / "data" / "siniestros_perico_modelable.csv"
RUTA_MODELO = RAIZ / "data" / "modelo_riesgo.pkl"
RUTA_METRICAS = RAIZ / "data" / "metricas_modelos.json"

CAT_COLS = [
    "modo_desplazamiento_victima",
    "contraparte_siniestro",
    "tipo_de_via",
    "dia_semana_nombre",
    "periodo_dia_nombre",
    "calle_1",
]
NUM_COLS = [
    "hora_num",
    "edad_victima",
    "total_victimas",
    "es_dia_feria",
    "es_feriado",
    "fin_semana",
    "es_post_semaforos",
]
TARGET = "target_es_grave"

# Etiquetas legibles para el grafico de feature importance
LABELS_COL = {
    "modo_desplazamiento_victima": "Modo de la víctima",
    "contraparte_siniestro": "Contraparte",
    "tipo_de_via": "Tipo de vía",
    "dia_semana_nombre": "Día de la semana",
    "periodo_dia_nombre": "Franja horaria",
    "calle_1": "Arteria",
    "hora_num": "Hora del día",
    "edad_victima": "Edad de la víctima",
    "total_victimas": "Total de víctimas",
    "es_dia_feria": "Día de feria",
    "es_feriado": "Feriado",
    "fin_semana": "Fin de semana",
    "es_post_semaforos": "Intersección semaforizada",
}

MODELOS_BASE = {
    "arbol": DecisionTreeClassifier(random_state=42),
    "random_forest": RandomForestClassifier(n_jobs=-1, random_state=42),
    "gradient_boosting": GradientBoostingClassifier(random_state=42),
}

# Grillas de hiperparametros (prefijo clf__ = paso del Pipeline)
GRIDS = {
    "arbol": {
        "clf__criterion": ["gini", "entropy"],
        "clf__max_depth": [4, 6, 8, 12, None],
        "clf__min_samples_split": [2, 5, 10],
        "clf__class_weight": ["balanced", None],
    },
    "random_forest": {
        "clf__n_estimators": [100, 150, 200],
        "clf__max_depth": [6, 10, None],
        "clf__min_samples_split": [2, 5],
        "clf__class_weight": ["balanced", None],
    },
    "gradient_boosting": {
        "clf__n_estimators": [100, 200],
        "clf__learning_rate": [0.05, 0.1],
        "clf__max_depth": [3, 5],
    },
}

CV = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)


def cargar_dataset():
    df = pd.read_csv(RUTA_CSV, encoding="utf-8-sig")
    for c in NUM_COLS + [TARGET]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df[TARGET] = df[TARGET].fillna(0).astype(int)
    for c in CAT_COLS:
        df[c] = df[c].fillna("SIN DATOS").astype(str).str.strip()
    return df


def construir_pipeline(clf):
    """One-Hot para categoricas + imputacion por mediana en numericas."""
    pre = ColumnTransformer(
        [
            ("cat", OneHotEncoder(handle_unknown="ignore"), CAT_COLS),
            ("num", SimpleImputer(strategy="median"), NUM_COLS),
        ]
    )
    return Pipeline([("pre", pre), ("clf", clf)])


def metricas_de(y_test, pred):
    return {
        "accuracy": round(float(accuracy_score(y_test, pred)), 4),
        "precision": round(float(precision_score(y_test, pred, zero_division=0)), 4),
        "recall": round(float(recall_score(y_test, pred, zero_division=0)), 4),
        "f1": round(float(f1_score(y_test, pred, zero_division=0)), 4),
        # [[TN, FP], [FN, TP]]
        "matriz_confusion": confusion_matrix(y_test, pred).tolist(),
    }


def _nombre_legible(feat):
    """'modo_desplazamiento_victima_MOTO' -> 'Modo de la víctima: MOTO'."""
    for col in CAT_COLS:
        if feat.startswith(col + "_"):
            return f"{LABELS_COL[col]}: {feat[len(col) + 1:]}"
    return LABELS_COL.get(feat, feat)


def main():
    df = cargar_dataset()
    X, y = df[CAT_COLS + NUM_COLS], df[TARGET]
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=42, stratify=y
    )

    resultado = {
        "fecha_entrenamiento": datetime.now().isoformat(timespec="seconds"),
        "n_muestras": int(len(df)),
        "split": "80/20 estratificado (random_state=42)",
        "optimizacion": "GridSearchCV · StratifiedKFold 5 folds · refit F1",
        "target": TARGET,
        "positivos_pct": round(100 * float(y.mean()), 1),
        "categorias": {
            "calle_1": df["calle_1"].value_counts().index.tolist(),
        },
        "modelos": {},
    }

    pipelines = {}
    for nombre, clf in MODELOS_BASE.items():
        pipe = construir_pipeline(clf)
        gs = GridSearchCV(
            pipe,
            GRIDS[nombre],
            scoring={"accuracy": "accuracy", "f1": "f1"},
            refit="f1",
            cv=CV,
            n_jobs=-1,
        )
        gs.fit(X_train, y_train)
        mejor_pipe = gs.best_estimator_
        pipelines[nombre] = mejor_pipe

        pred = mejor_pipe.predict(X_test)
        resultado["modelos"][nombre] = {
            **metricas_de(y_test, pred),
            "f1_cv": round(float(gs.best_score_), 4),
            "mejores_params": {
                k.replace("clf__", ""): (v if v is not None else "None")
                for k, v in gs.best_params_.items()
            },
        }
        print(f"[{nombre}] params={gs.best_params_} f1_cv={gs.best_score_:.3f}")

    # Badge academico: mejor F1 en test. Para inferencia publica se sirve el
    # mejor ENSAMBLE (RF/GB): produce probabilidades mas suaves que el arbol
    # (que tiende a extremos 0%/100% en hojas puras).
    mejor = max(resultado["modelos"],
                key=lambda m: resultado["modelos"][m]["f1"])
    ensambles = ("random_forest", "gradient_boosting")
    mejor_ensamble = max(ensambles,
                         key=lambda m: resultado["modelos"][m]["f1"])
    resultado["mejor_modelo"] = mejor
    resultado["modelo_inferencia"] = mejor_ensamble

    # Feature importance del mejor modelo (todos los candidatos la exponen)
    pipe_fi = pipelines[mejor]
    feats = pipe_fi.named_steps["pre"].get_feature_names_out()
    feats = [f.split("__", 1)[-1] for f in feats]
    imps = pipe_fi.named_steps["clf"].feature_importances_
    resultado["feature_importance"] = [
        {
            "feature": f,
            "label": _nombre_legible(f),
            "importance": round(float(i), 4),
        }
        for f, i in sorted(zip(feats, imps), key=lambda t: -t[1])[:15]
        if i > 0
    ]
    resultado["importancia_de"] = mejor

    joblib.dump(
        {
            "pipelines": pipelines,
            "cat_cols": CAT_COLS,
            "num_cols": NUM_COLS,
            "mejor_modelo": mejor,
            "modelo_inferencia": mejor_ensamble,
        },
        RUTA_MODELO,
    )
    with open(RUTA_METRICAS, "w", encoding="utf-8") as f:
        json.dump(resultado, f, ensure_ascii=False, indent=2)

    print(f"\nMuestras: {len(df)} | positivos (graves): {y.mean():.1%}")
    for nombre, m in resultado["modelos"].items():
        marca = " <- MEJOR" if nombre == mejor else ""
        print(
            f"  {nombre:18s} acc={m['accuracy']:.3f} P={m['precision']:.3f} "
            f"R={m['recall']:.3f} F1={m['f1']:.3f} (f1_cv={m['f1_cv']:.3f}){marca}"
        )
    print(f"Inferencia  -> {mejor_ensamble} (ensamble mejor calibrado)")
    print(f"Modelo      -> {RUTA_MODELO.name}")
    print(f"Metricas    -> {RUTA_METRICAS.name}")


if __name__ == "__main__":
    main()
