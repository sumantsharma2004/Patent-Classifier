import streamlit as st
import pandas as pd
from openai import AzureOpenAI
import time
from typing import Dict, List
import json
import io
import re

# Page configuration
st.set_page_config(
    page_title="IeB Classifier",
    page_icon="🔬",
    layout="wide"
)

# Initialize session state for user-specific data
# IMPORTANT: Each user session has isolated credentials and data
# This prevents cross-user data leakage in multi-user deployments
if 'results_df' not in st.session_state:
    st.session_state.results_df = None
if 'processing_complete' not in st.session_state:
    st.session_state.processing_complete = False
if 'credentials' not in st.session_state:
    st.session_state.credentials = {
        'api_key': '',
        'endpoint': '',
        'api_version': '2025-01-01-preview',
        'model': 'gpt-4o'
    }

def consolidate_patent_records(df: pd.DataFrame) -> pd.DataFrame:
    """
    Consolidate multi-row patent records into single rows.
    Handles cases where descriptions are split across multiple rows.
    """
    # Pattern to detect a new publication number (US, CN, KR, EP, WO, JP etc.)
    pub_pattern = re.compile(r'^[A-Z]{2}\d+')

    final_rows = []
    current_record = None

    # Find the actual column names in the dataframe
    pub_col = None
    title_col = None
    abstract_col = None
    claims_col = None
    desc_col = None

    for col in df.columns:
        col_lower = col.strip().lower()
        if 'publication' in col_lower and 'number' in col_lower:
            pub_col = col
        elif col_lower == 'title':
            title_col = col
        elif 'abstract' in col_lower:
            abstract_col = col
        elif 'claim' in col_lower:
            claims_col = col
        elif 'description' in col_lower:
            desc_col = col

    # If we can't find a publication number column, return as-is
    if not pub_col:
        return df

    for _, row in df.iterrows():
        pub_no = str(row[pub_col]).strip() if pub_col else ""

        # If row begins with a valid publication number – start a new block
        if pub_pattern.match(pub_no):
            # Save previous record
            if current_record:
                final_rows.append(current_record)

            # Start new record
            current_record = {
                "publication number": pub_no,
                "title": row.get(title_col, "") if title_col else "",
                "abstract": row.get(abstract_col, "") if abstract_col else "",
                "claims": row.get(claims_col, "") if claims_col else "",
                "description": str(row.get(desc_col, "")) if desc_col else ""
            }

        else:
            # This is continuation text – append to description field
            if current_record and desc_col:
                extra = str(row.get(desc_col, "")).strip()
                if extra and extra != 'nan':
                    current_record["description"] += "\n" + extra

    # Save last record
    if current_record:
        final_rows.append(current_record)

    # Convert to DataFrame
    if final_rows:
        return pd.DataFrame(final_rows)
    else:
        return df

def get_azure_client(api_key: str, endpoint: str, api_version: str):
    """
    Initialize Azure OpenAI client with provided credentials.

    Credentials are passed explicitly (not from environment variables)
    to ensure proper session isolation in multi-user deployments.
    """
    return AzureOpenAI(
        api_key=api_key,
        api_version=api_version,
        azure_endpoint=endpoint
    )

def create_classification_prompt(prompt_template: str, row_data: Dict) -> str:
    """Create the prompt by substituting placeholders with actual data"""
    prompt = prompt_template
    for key, value in row_data.items():
        placeholder = f"{{{key}}}"
        prompt = prompt.replace(placeholder, str(value) if pd.notna(value) else 'N/A')
    return prompt

def classify_patent(row: pd.Series, client, model: str, prompt_template: str, column_mapping: Dict) -> Dict:
    """Send patent data to Azure OpenAI for classification"""

    # Create row data dictionary with mapped columns
    row_data = {}
    for standard_name, actual_column in column_mapping.items():
        row_data[standard_name] = row.get(actual_column, 'N/A')

    # Create the prompt with patent data
    prompt = create_classification_prompt(prompt_template, row_data)

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are a quantum technology expert specializing in patent analysis."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3,
        )

        # Parse the response
        response_text = response.choices[0].message.content.strip()

        # Try to extract JSON from the response
        if "```json" in response_text:
            response_text = response_text.split("```json")[1].split("```")[0].strip()
        elif "```" in response_text:
            response_text = response_text.split("```")[1].split("```")[0].strip()

        result = json.loads(response_text)
        return result

    except json.JSONDecodeError as e:
        return {
            "relevance": "ERROR",
            "relevance_percentage": 0,
            "confidence": "N/A",
            "reasoning": f"Failed to parse response: {response_text[:200]}",
            "key_features_found": [],
            "protocols_mentioned": [],
            "relevance_source": "N/A"
        }
    except Exception as e:
        return {
            "relevance": "ERROR",
            "relevance_percentage": 0,
            "confidence": "N/A",
            "reasoning": f"API error: {str(e)}",
            "key_features_found": [],
            "protocols_mentioned": [],
            "relevance_source": "N/A"
        }

def process_patents(df: pd.DataFrame, client, model: str, prompt_template: str,
                   column_mapping: Dict, progress_bar, status_text):
    """Process all patents in the dataframe"""
    results = []
    total_rows = len(df)

    for idx, row in df.iterrows():
        status_text.text(f"Processing patent {idx + 1}/{total_rows}: {row.get(column_mapping.get('publication_number', ''), 'N/A')}")

        # Classify the patent
        classification = classify_patent(row, client, model, prompt_template, column_mapping)

        # Add classification results to the row
        result_row = row.to_dict()
        result_row['relevance'] = classification['relevance']
        result_row['relevance_percentage'] = classification.get('relevance_percentage', 0)
        result_row['confidence'] = classification['confidence']
        result_row['reasoning'] = classification['reasoning']
        result_row['key_features_found'] = ', '.join(classification['key_features_found'])
        result_row['protocols_mentioned'] = ', '.join(classification['protocols_mentioned'])
        result_row['relevance_source'] = classification.get('relevance_source', 'N/A')

        results.append(result_row)

        # Update progress
        progress_bar.progress((idx + 1) / total_rows)

        # Add a small delay to avoid rate limiting
        time.sleep(0.5)

    return pd.DataFrame(results)

# Default prompt template
DEFAULT_PROMPT = """You are a quantum communication technology expert. Analyze the following patent to determine if it is relevant to a specific QKD (Quantum Key Distribution) last-mile problem system.

**TARGET SYSTEM CRITERIA:**
A QKD system for last-mile problem consisting of:
- Entangled single photons transmitted to generate a shared cryptographic key that is immediately available


**RELEVANT KEYWORDS & SYNONYMS:**

1. Quantum Key Distribution (QKD): Quantum cryptographic communication, quantum key exchange, quantum encryption protocol, quantum-secured communication system, quantum information transmission system, quantum-based key establishment, quantum communication framework, quantum secret sharing mechanism

2. Device (Alice/Bob): Node, terminal, endpoint, station, transceiver unit, communication module, quantum apparatus, participant unit, sender, receiver

3. Optical Fiber: Fiber-optic link, optical waveguide, light-transmission medium, photonic channel, fiber channel, optical transmission line, glass fiber cable, optical conduit

4. Transmit Data: Exchange information, communicate signals, send and receive data, convey information, transfer data packets, propagate signals, relay quantum states, communicate bit sequences

5. Bidirectionally: In both directions, duplex mode, two-way communication, reciprocal transmission, mutual data exchange, dual-directional signaling, reverse and forward channels, full-duplex mode

6. Photons: Quantum light particles, light quanta, optical qubits, single-photon pulses, quantum carriers, photon packets, quantum optical signals, entangled light particles

7. Shared Cryptographic Key: Secret key, mutual encryption key, joint generation key, symmetric key, symmetric encryption key, session key, private key, quantum-generated key, secure shared key, common cryptographic token, co-established encryption key, secure key

8. Entangled: Correlated, quantum-linked, nonlocally connected, coherently coupled, quantum-correlated, interdependent (quantum state), superposition-coupled, BBM92, E91
**PATENT INFORMATION:**
Publication Number: {publication_number}
Title: {title}
Abstract: {abstract}
Claims: {claims}
Description: {description}


**TASK:**
Analyze this patent and respond with ONLY a JSON object in the following format:
{{
   "relevance": "RELEVANT" or "NOT RELEVANT",
   "relevance_percentage": <number between 0-100>,
   "confidence": "HIGH", "MEDIUM", or "LOW",
   "reasoning": "Brief explanation (2-3 sentences) of why this patent is relevant or not relevant",
   "key_features_found": ["list of key QKD features identified in the patent"],
   "protocols_mentioned": ["E91", "BBM92", "B92"] (only include these 3 protocols if explicitly mentioned or clearly described),
   "relevance_source": "CLAIMS" or "DESCRIPTION" or "BOTH" or "ABSTRACT" or "TITLE" (indicate where the key relevant information was found that led to the relevance decision)
}}

IMPORTANT CLASSIFICATION RULES:
- Set "relevance" to "RELEVANT" ONLY if relevance_percentage is 75% or higher
- Set "relevance" to "NOT RELEVANT" if relevance_percentage is below 75%

The relevance_percentage should be:
- 0-20%: Not relevant at all - no QKD features present
- 21-40%: Minimally relevant - mentions quantum but not QKD specific
- 41-60%: Somewhat relevant - QKD related but not entanglement-based
- 61-74%: Moderately relevant - QKD with some features but missing critical elements
- 75-89%: Highly relevant - QKD with entanglement and most key features
- 90-100%: Extremely relevant - matches all target system criteria including E91/BBM92/B92 protocols

Confidence levels should reflect:
- HIGH: Very clear evidence for the score, explicit mentions of key features
- MEDIUM: Good evidence but some ambiguity or missing details
- LOW: Uncertain, limited information, or difficult to assess

Respond ONLY with the JSON object, no additional text.
"""

# Main UI
st.title("🔬 IeB Classifier")
st.markdown("Analyze patents for your task and check the relevancy through AI")

# Sidebar for configuration
st.sidebar.header("⚙️ Configuration")

# Azure OpenAI Configuration
st.sidebar.subheader("Azure OpenAI Settings")

# Security info
st.sidebar.info("🔒 **Secure Multi-User Support**: Your credentials are isolated to your session only and not shared with other users.")

# Option to upload .env file for easy credential loading
st.sidebar.markdown("**Option 1: Upload .env file**")
env_file = st.sidebar.file_uploader(
    "Upload .env file (optional)",
    type=['env', 'txt'],
    help="Upload your .env file to automatically populate credentials"
)

# Parse .env file if uploaded
if env_file is not None:
    env_content = env_file.read().decode('utf-8')
    for line in env_content.splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")

            if key == "AZURE_API_KEY":
                st.session_state.credentials['api_key'] = value
            elif key == "AZURE_ENDPOINT":
                st.session_state.credentials['endpoint'] = value
            elif key == "AZURE_API_VERSION":
                st.session_state.credentials['api_version'] = value
            elif key == "AZURE_MODEL":
                st.session_state.credentials['model'] = value

    st.sidebar.success("✅ Credentials loaded from .env file")

st.sidebar.markdown("**Option 2: Enter credentials manually**")

# Input fields using session state
azure_api_key = st.sidebar.text_input(
    "API Key",
    type="password",
    value=st.session_state.credentials['api_key'],
    placeholder="Enter your Azure OpenAI API key",
    key="api_key_input"
)

azure_endpoint = st.sidebar.text_input(
    "Endpoint URL",
    value=st.session_state.credentials['endpoint'],
    placeholder="https://your-resource.openai.azure.com/",
    key="endpoint_input"
)

azure_api_version = st.sidebar.text_input(
    "API Version",
    value=st.session_state.credentials['api_version'],
    placeholder="2025-01-01-preview",
    key="api_version_input"
)

azure_model = st.sidebar.text_input(
    "Model Deployment Name",
    value=st.session_state.credentials['model'],
    placeholder="gpt-4o",
    key="model_input"
)

# Update session state with current input values
st.session_state.credentials['api_key'] = azure_api_key
st.session_state.credentials['endpoint'] = azure_endpoint
st.session_state.credentials['api_version'] = azure_api_version
st.session_state.credentials['model'] = azure_model

# File upload section
st.header("📁 Upload Patent Data")

# Option to consolidate multi-row records
consolidate_records = st.checkbox(
    "Consolidate multi-row patent records",
    value=False,
    help="Enable this if your CSV has patent descriptions split across multiple rows. This will merge them into single records."
)

uploaded_file = st.file_uploader(
    "Choose a file (CSV, TSV, or Excel)",
    type=['csv', 'tsv', 'xlsx', 'xls'],
    help="Upload your patent data file containing publication numbers, titles, abstracts, claims, and descriptions"
)

if uploaded_file is not None:
    # Read the file based on its type
    try:
        file_extension = uploaded_file.name.split('.')[-1].lower()

        if file_extension == 'csv':
            df = pd.read_csv(uploaded_file)
        elif file_extension == 'tsv':
            df = pd.read_csv(uploaded_file, sep='\t')
        elif file_extension in ['xlsx', 'xls']:
            df = pd.read_excel(uploaded_file)
        else:
            st.error("Unsupported file format!")
            st.stop()

        # Normalize column names
        df.columns = df.columns.str.strip().str.lower()

        original_row_count = len(df)

        # Consolidate multi-row records if enabled
        if consolidate_records:
            with st.spinner("Consolidating multi-row patent records..."):
                df = consolidate_patent_records(df)
                st.info(f"📋 Consolidated {original_row_count} rows into {len(df)} patent records")

        st.success(f"✅ File loaded successfully! Found {len(df)} rows and {len(df.columns)} columns")

        # Show preview
        with st.expander("📊 Preview Data (first 5 rows)"):
            st.dataframe(df.head())

        # Option to download consolidated data
        if consolidate_records and len(df) < original_row_count:
            st.subheader("💾 Download Consolidated Data")
            col1, col2 = st.columns(2)

            with col1:
                # TSV download
                tsv_data = df.to_csv(sep='\t', index=False)
                st.download_button(
                    label="Download Consolidated TSV",
                    data=tsv_data,
                    file_name="consolidated_patents.tsv",
                    mime="text/tab-separated-values",
                    help="Download the consolidated patent data as TSV"
                )

            with col2:
                # CSV download
                csv_data = df.to_csv(index=False)
                st.download_button(
                    label="Download Consolidated CSV",
                    data=csv_data,
                    file_name="consolidated_patents.csv",
                    mime="text/csv",
                    help="Download the consolidated patent data as CSV"
                )

        # Column mapping section
        st.header("🔗 Column Mapping")
        st.markdown("Map your file's columns to the required fields. Use the placeholders `{publication_number}`, `{title}`, `{abstract}`, `{claims}`, `{description}` in your prompt.")

        available_columns = df.columns.tolist()

        col1, col2 = st.columns(2)

        with col1:
            st.subheader("Required Fields")
            publication_number_col = st.selectbox(
                "Publication Number Column",
                options=available_columns,
                index=available_columns.index('publication number') if 'publication number' in available_columns else 0
            )
            title_col = st.selectbox(
                "Title Column",
                options=available_columns,
                index=available_columns.index('title') if 'title' in available_columns else 0
            )
            abstract_col = st.selectbox(
                "Abstract Column",
                options=available_columns,
                index=available_columns.index('abstract') if 'abstract' in available_columns else 0
            )

        with col2:
            st.subheader("Optional Fields")
            claims_col = st.selectbox(
                "Claims Column",
                options=['None'] + available_columns,
                index=available_columns.index('claims') + 1 if 'claims' in available_columns else 0
            )
            description_col = st.selectbox(
                "Description Column",
                options=['None'] + available_columns,
                index=available_columns.index('description') + 1 if 'description' in available_columns else 0
            )

        # Create column mapping
        column_mapping = {
            'publication_number': publication_number_col,
            'title': title_col,
            'abstract': abstract_col,
            'claims': claims_col if claims_col != 'None' else '',
            'description': description_col if description_col != 'None' else ''
        }

        # Prompt configuration
        st.header("📝 Prompt Configuration")
        prompt_choice = st.radio(
            "Choose prompt option:",
            ["Use Default Prompt", "Customize Prompt"]
        )

        if prompt_choice == "Use Default Prompt":
            prompt_template = DEFAULT_PROMPT
            with st.expander("View Default Prompt"):
                st.text_area("Default Prompt Template", value=DEFAULT_PROMPT, height=300, disabled=True)
        else:
            st.markdown("""
            **Available placeholders:**
            - `{publication_number}` - Publication number
            - `{title}` - Patent title
            - `{abstract}` - Patent abstract
            - `{claims}` - Patent claims
            - `{description}` - Patent description
            """)
            prompt_template = st.text_area(
                "Custom Prompt Template",
                value=DEFAULT_PROMPT,
                height=400,
                help="Use the placeholders above to customize your prompt"
            )

        # Process button
        st.header("🚀 Process Patents")

        col1, col2, col3 = st.columns([1, 1, 2])
        with col1:
            process_button = st.button("Start Analysis", type="primary", use_container_width=True)
        with col2:
            if st.session_state.processing_complete:
                st.success("✅ Processing Complete!")

        if process_button:
            if not azure_api_key or not azure_endpoint:
                st.error("⚠️ Please provide Azure OpenAI credentials in the sidebar!")
            else:
                try:
                    # Initialize Azure OpenAI client
                    client = get_azure_client(azure_api_key, azure_endpoint, azure_api_version)

                    # Create progress indicators
                    progress_bar = st.progress(0)
                    status_text = st.empty()

                    # Process patents
                    with st.spinner("Processing patents..."):
                        results_df = process_patents(
                            df,
                            client,
                            azure_model,
                            prompt_template,
                            column_mapping,
                            progress_bar,
                            status_text
                        )

                    st.session_state.results_df = results_df
                    st.session_state.processing_complete = True

                    status_text.text("✅ All patents processed!")
                    progress_bar.progress(1.0)

                    st.success("🎉 Analysis complete! See results below.")

                except Exception as e:
                    st.error(f"❌ Error during processing: {str(e)}")

        # Display results
        if st.session_state.results_df is not None:
            results_df = st.session_state.results_df

            st.header("📊 Results")

            # Summary statistics
            st.subheader("Summary Statistics")

            col1, col2, col3, col4 = st.columns(4)

            with col1:
                total_patents = len(results_df)
                st.metric("Total Patents", total_patents)

            with col2:
                relevant_count = len(results_df[results_df['relevance'] == 'RELEVANT'])
                st.metric("Relevant Patents", relevant_count, f"{(relevant_count/total_patents*100):.1f}%")

            with col3:
                avg_relevance = results_df['relevance_percentage'].mean()
                st.metric("Avg Relevance Score", f"{avg_relevance:.1f}%")

            with col4:
                high_confidence = len(results_df[results_df['confidence'] == 'HIGH'])
                st.metric("High Confidence", high_confidence)

            # Charts
            col1, col2 = st.columns(2)

            with col1:
                st.subheader("Relevance Distribution")
                relevance_counts = results_df['relevance'].value_counts()
                st.bar_chart(relevance_counts)

            with col2:
                st.subheader("Confidence Distribution")
                confidence_counts = results_df['confidence'].value_counts()
                st.bar_chart(confidence_counts)

            # Relevant patents details
            st.subheader("Relevant Patents")
            relevant_patents = results_df[results_df['relevance'] == 'RELEVANT'].sort_values('relevance_percentage', ascending=False)

            if len(relevant_patents) > 0:
                for idx, patent in relevant_patents.iterrows():
                    with st.expander(f"📄 {patent.get(column_mapping['publication_number'], 'N/A')} - {patent.get('relevance_percentage', 0):.0f}%"):
                        col1, col2 = st.columns([2, 1])

                        with col1:
                            st.write(f"**Title:** {patent.get(column_mapping['title'], 'N/A')}")
                            st.write(f"**Reasoning:** {patent.get('reasoning', 'N/A')}")
                            if patent.get('key_features_found'):
                                st.write(f"**Key Features:** {patent.get('key_features_found')}")
                            if patent.get('protocols_mentioned'):
                                st.write(f"**Protocols:** {patent.get('protocols_mentioned')}")

                        with col2:
                            st.metric("Relevance", f"{patent.get('relevance_percentage', 0):.0f}%")
                            st.write(f"**Confidence:** {patent.get('confidence', 'N/A')}")
                            st.write(f"**Source:** {patent.get('relevance_source', 'N/A')}")
            else:
                st.info("No relevant patents found.")

            # Full results table
            st.subheader("All Results")
            st.dataframe(results_df, use_container_width=True)

            # Download section
            st.subheader("💾 Download Results")

            col1, col2 = st.columns(2)

            with col1:
                # Excel download
                output = io.BytesIO()
                with pd.ExcelWriter(output, engine='openpyxl') as writer:
                    results_df.to_excel(writer, index=False, sheet_name='Results')
                output.seek(0)

                st.download_button(
                    label="Download as Excel",
                    data=output,
                    file_name="qkd_patent_analysis_results.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )

            with col2:
                # CSV download
                csv = results_df.to_csv(index=False)
                st.download_button(
                    label="Download as CSV",
                    data=csv,
                    file_name="qkd_patent_analysis_results.csv",
                    mime="text/csv"
                )

    except Exception as e:
        st.error(f"❌ Error reading file: {str(e)}")
        st.stop()

else:
    st.info("👆 Please upload a file to get started")

    # Show instructions
    with st.expander("ℹ️ How to use this tool"):
        st.markdown("""
        ### Instructions:

        1. **Configure Azure OpenAI Settings** (in sidebar):
           - Enter your API Key
           - Enter your Endpoint URL
           - Verify API Version and Model Name

        2. **Upload Your Patent Data**:
           - Supported formats: CSV, TSV, Excel (.xlsx, .xls)
           - File should contain patent information columns
           - **Enable "Consolidate multi-row patent records"** if your CSV has patent descriptions split across multiple rows (common with patent database exports)

        3. **Map Columns**:
           - Select which columns in your file correspond to:
             - Publication Number
             - Title
             - Abstract
             - Claims (optional)
             - Description (optional)

        4. **Configure Prompt** (optional):
           - Use the default QKD analysis prompt
           - Or customize it for your specific needs
           - Use placeholders like `{publication_number}`, `{title}`, etc.

        5. **Start Analysis**:
           - Click "Start Analysis" button
           - Monitor progress as patents are analyzed
           - View results and statistics

        6. **Download Results**:
           - Export results as Excel or CSV
           - Results include relevance scores, confidence levels, and reasoning
        """)

# Footer
st.markdown("---")
st.markdown("Made with Streamlit | QKD Patent Analyzer v1.0")
